"""Multi-POV audio alignment: find the time offset between two friends' recordings of the same moment.

Player A's voice is on A's mic track and, a Discord hop later, on B's voice-chat track. Cross-correlating
the two gives `t_b - t_a + latency`. Doing the reverse pair too (B's mic against A's voice track) gives
`t_a - t_b + latency`, so the mean of the two cancels the Discord latency and their half-difference
estimates it.

Two passes, numpy only:

1. **Coarse:** speech-band (300-3400 Hz) log-energy onset envelopes at 100 Hz, correlated over the
   whole files with one FFT (an hour is 360k samples, so no windowing is needed here). Onsets ignore
   level, codec colouring and steady game noise. Confidence comes from this pass: how far the peak
   stands above the correlation's noise floor (robust z) and above the best rival peak (ratio).
2. **Fine:** band-passed waveform cross-correlation in the loudest few seconds-long windows of the mic,
   +-60 ms around the coarse lag. Used only when the windows agree; otherwise the coarse lag stands.

Offset convention: `offset_s = t_b - t_a`, i.e. something at time t in recording A is at t + offset_s in
recording B.
"""

from __future__ import annotations

import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .media import MediaError, audio_tracks, extract_audio

SR = 16_000  # extract_audio's output rate
HOP = 160  # 10 ms envelope hop
FRAME = 400  # 25 ms analysis frame
ENV_RATE = SR / HOP
BAND_HZ = (300.0, 3400.0)

# 1-based audio track numbers per recorder layout (what a_mic_track etc. take).
OBS_TRACKS = {"mix": 1, "game": 2, "voice": 3, "mic": 4}  # OBS stopgap: 3 is Discord
SEGRA_TRACKS = {"mix": 1, "mic": 2, "game": 3, "voice": 4}  # Segra fork (one mic)

# Confidence calibration (coarse pass). Unrelated audio peaks at z 5.7-8.4 with a rival ratio 1.0-1.35;
# matches seen in synthetic Discord-damaged scenes sit at z 15-24, ratio 2.1-4 (tests/test_align.py).
_Z_LO, _Z_HI = 9.0, 14.0
_R_LO, _R_HI = 1.4, 2.0
_EXCLUDE_S = 0.3  # rival peaks must be at least this far from the main one
_FINE_WIN_S = 4.0
_FINE_MARGIN_S = 0.06
_FINE_WINDOWS = 6


@dataclass(frozen=True)
class Alignment:
    offset_s: float  # t_b - t_a
    confidence: float  # 0..1
    method: str  # "coarse", "fine", "both-directions", "none"
    details: dict = field(default_factory=dict)


# ---------------------------------------------------------------- features


def _as_float(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x)
    if x.dtype == np.int16:
        return x.astype(np.float32) / 32768.0
    return x.astype(np.float32, copy=False)


def onset_envelope(x: np.ndarray, sr: int = SR) -> np.ndarray:
    """Half-wave-rectified first difference of speech-band log energy, one value per 10 ms hop."""
    hop, frame = round(sr / ENV_RATE), round(sr * FRAME / SR)
    n = 1 + (len(x) - frame) // hop if len(x) >= frame else 0
    if n < 2:
        return np.zeros(0, np.float32)
    freqs = np.fft.rfftfreq(frame, 1.0 / sr)
    band = (freqs >= BAND_HZ[0]) & (freqs <= BAND_HZ[1])
    win = np.hanning(frame).astype(np.float32)
    energy = np.empty(n, np.float64)
    step = 6000  # frames per chunk (60 s), bounds memory for hour-long tracks
    for s in range(0, n, step):
        e = min(n, s + step)
        idx = np.arange(s, e)[:, None] * hop + np.arange(frame)[None, :]
        spec = np.fft.rfft(_as_float(x[idx]) * win, axis=1)
        energy[s:e] = (np.abs(spec[:, band]) ** 2).sum(axis=1)
    # Floor relative to the loud part so digital silence and noise-gated stretches don't create onsets.
    floor = max(1e-10, 1e-3 * float(np.percentile(energy, 95)))
    loge = np.log(energy + floor)
    onset = np.maximum(0.0, np.diff(loge, prepend=loge[0]))
    return onset.astype(np.float32)


def _xcorr(a: np.ndarray, b: np.ndarray, max_lag: int) -> tuple[np.ndarray, np.ndarray]:
    """c[k] = sum_t a[t] * b[t + k] for k in [-max_lag, max_lag] (clipped to what overlaps)."""
    n = len(a) + len(b)
    nfft = 1 << (n - 1).bit_length()
    c = np.fft.irfft(np.fft.rfft(b, nfft) * np.conj(np.fft.rfft(a, nfft)), nfft)
    lo, hi = max(-max_lag, -(len(a) - 1)), min(max_lag, len(b) - 1)
    lags = np.arange(lo, hi + 1)
    return lags, c[lags % nfft]


def _parabolic(y: np.ndarray, i: int) -> float:
    if 0 < i < len(y) - 1:
        d = y[i - 1] - 2 * y[i] + y[i + 1]
        if d < 0:
            return float(np.clip(0.5 * (y[i - 1] - y[i + 1]) / d, -0.5, 0.5))
    return 0.0


def _ramp(v: float, lo: float, hi: float) -> float:
    return float(np.clip((v - lo) / (hi - lo), 0.0, 1.0))


# ---------------------------------------------------------------- passes


def _coarse(ea: np.ndarray, eb: np.ndarray, max_offset_s: float) -> dict:
    za = (ea - ea.mean()) / (ea.std() + 1e-12)
    zb = (eb - eb.mean()) / (eb.std() + 1e-12)
    lags, c = _xcorr(za, zb, int(round(max_offset_s * ENV_RATE)))
    c = c / np.sqrt(len(za) * len(zb))
    i = int(np.argmax(c))
    med = float(np.median(c))
    mad = float(np.median(np.abs(c - med))) * 1.4826 + 1e-12
    excl = int(_EXCLUDE_S * ENV_RATE)
    rivals = np.concatenate([c[: max(0, i - excl)], c[i + excl + 1 :]])
    second = float(rivals.max()) if len(rivals) else med
    z = (float(c[i]) - med) / mad
    ratio = (float(c[i]) - med) / max(second - med, 1e-12)
    lag_s = (lags[i] + _parabolic(c, i)) / ENV_RATE
    conf = _ramp(z, _Z_LO, _Z_HI) * _ramp(ratio, _R_LO, _R_HI)
    return {"lag_s": float(lag_s), "z": round(z, 2), "ratio": round(ratio, 3),
            "peak": round(float(c[i]), 4), "confidence": round(conf, 3)}  # fmt: skip


def _bandpass(x: np.ndarray, sr: int) -> np.ndarray:
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1.0 / sr)
    spec[(f < BAND_HZ[0]) | (f > BAND_HZ[1])] = 0
    return np.fft.irfft(spec, len(x))


def _fine(mic: np.ndarray, voice: np.ndarray, sr: int, coarse_lag_s: float, env: np.ndarray) -> dict:
    win, margin = int(_FINE_WIN_S * sr), int(_FINE_MARGIN_S * sr)
    lag0 = int(round(coarse_lag_s * sr))
    # Candidate windows: the mic's busiest stretches (onset mass), non-overlapping, fully inside B.
    wenv = int(_FINE_WIN_S * ENV_RATE)
    if len(env) < wenv:
        return {"used": False, "reason": "too short"}
    mass = np.convolve(env, np.ones(wenv), "valid")
    starts: list[int] = []
    for k in np.argsort(mass)[::-1]:
        s = int(k * HOP * sr / SR)
        if s + lag0 - margin < 0 or s + lag0 + win + margin > len(voice) or s + win > len(mic):
            continue
        if any(abs(s - t) < win for t in starts):
            continue
        starts.append(s)
        if len(starts) == _FINE_WINDOWS:
            break
    found: list[tuple[float, float]] = []
    for s in starts:
        a = _bandpass(_as_float(mic[s : s + win]).astype(np.float64), sr)
        b = _bandpass(_as_float(voice[s + lag0 - margin : s + lag0 + win + margin]).astype(np.float64), sr)
        lags, c = _xcorr(a, b, 2 * margin)
        keep = (lags >= 0) & (lags <= 2 * margin)  # b starts `margin` early, so lag `margin` == lag0
        lags, c = lags[keep], c[keep]
        # Normalise by the energy of the b slice each lag actually overlaps (cumulative sums).
        cs = np.concatenate([[0.0], np.cumsum(b * b)])
        eb = cs[lags + win] - cs[lags]
        c = c / np.sqrt((a @ a) * eb + 1e-20)
        i = int(np.argmax(c))
        found.append(((lags[i] - margin + _parabolic(c, i)) / sr, float(c[i])))
    good = [(d, p) for d, p in found if p >= 0.2]
    if len(good) < 2:
        return {"used": False, "reason": "weak waveform match", "windows": len(found)}
    deltas = np.array([d for d, _ in good])
    med = float(np.median(deltas))
    agree = deltas[np.abs(deltas - med) <= 0.002]
    if len(agree) < max(2, len(good) // 2 + 1):
        return {
            "used": False,
            "reason": "windows disagree",
            "spread_ms": round(float(np.ptp(deltas)) * 1e3, 1),
        }
    return {"used": True, "lag_s": lag0 / sr + float(np.mean(agree)),
            "windows": len(agree), "peak": round(float(np.median([p for _, p in good])), 3)}  # fmt: skip


def align_arrays(
    mic: np.ndarray, voice: np.ndarray, sr: int = SR, *, max_offset_s: float = 300.0, fine: bool = True
) -> Alignment:
    """Lag of `mic`'s content inside `voice`: content at t in `mic` is at t + offset_s in `voice`.
    Arrays are mono int16 or float at `sr`."""
    ea, eb = onset_envelope(mic, sr), onset_envelope(voice, sr)
    if len(ea) < ENV_RATE or len(eb) < ENV_RATE or not ea.any() or not eb.any():
        return Alignment(0.0, 0.0, "none", {"reason": "too short or silent"})
    co = _coarse(ea, eb, max_offset_s)
    details: dict = {"coarse": co}
    offset, method = co["lag_s"], "coarse"
    conf = co["confidence"]
    if fine:
        fi = _fine(mic, voice, sr, co["lag_s"], ea)
        details["fine"] = fi
        if fi["used"] and abs(fi["lag_s"] - co["lag_s"]) <= _FINE_MARGIN_S:
            offset, method = fi["lag_s"], "fine"
            # Independent windows agreeing to 2 ms on the waveform is evidence of its own, but only when
            # the coarse peak was already the clear winner (a tone or loop could agree at a wrong lag).
            if co["ratio"] >= _R_LO:
                fconf = _ramp(fi["windows"], 2, 5) * _ramp(fi["peak"], 0.15, 0.3)
                conf = round(1 - (1 - conf) * (1 - fconf), 3)
    return Alignment(round(offset, 4), conf, method, details)


def combine(ab: Alignment, ba: Alignment) -> Alignment:
    """Merge A-mic->B-voice (`ab`, lag = offset + latency) with B-mic->A-voice (`ba`, lag = -offset +
    latency). Agreeing pairs cancel the Discord latency; otherwise the more confident one wins."""
    details = {"a_to_b": ab.offset_s, "b_to_a": ba.offset_s, "a_to_b_conf": ab.confidence,
               "b_to_a_conf": ba.confidence}  # fmt: skip
    offset, latency = (ab.offset_s - ba.offset_s) / 2, (ab.offset_s + ba.offset_s) / 2
    if ab.confidence > 0 and ba.confidence > 0 and -0.05 <= latency <= 1.0:
        conf = 1 - (1 - ab.confidence) * (1 - ba.confidence)
        return Alignment(round(offset, 4), round(conf, 3), "both-directions",
                         {**details, "latency_s": round(latency, 4)})  # fmt: skip
    best, sign = (ab, 1) if ab.confidence >= ba.confidence else (ba, -1)
    both_hit = min(ab.confidence, ba.confidence) > 0
    if both_hit:
        details["note"] = "directions disagree; latency not removed"
    else:
        details["note"] = "one direction only; offset includes the Discord latency"
    conf = best.confidence * (0.5 if both_hit else 1.0)
    return Alignment(round(sign * best.offset_s, 4), round(conf, 3), best.method, details)


# ---------------------------------------------------------------- files


def read_wav(path: Path | str) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise MediaError(f"{path}: expected mono s16 WAV")
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2"), w.getframerate()


def _track(path: Path | str, track: int, tmp: Path, name: str) -> np.ndarray:
    tracks = audio_tracks(path)
    if not 1 <= track <= len(tracks):
        raise MediaError(f"{path} has {len(tracks)} audio tracks; track {track} asked for")
    x, sr = read_wav(extract_audio(path, tracks[track - 1].index, tmp / f"{name}.wav"))
    assert sr == SR
    return x


def align(
    rec_a: Path | str,
    rec_b: Path | str,
    *,
    a_mic_track: int,
    b_voice_track: int,
    b_mic_track: int | None = None,
    a_voice_track: int | None = None,
    max_offset_s: float = 300.0,
) -> Alignment:
    """Offset (t_b - t_a) between two recordings. Track numbers are 1-based audio tracks (see
    OBS_TRACKS / SEGRA_TRACKS). Pass b_mic_track and a_voice_track as well to measure both directions,
    which removes the Discord latency from the offset. Originals are only read."""
    with tempfile.TemporaryDirectory(prefix="autoclipper-align-") as d:
        tmp = Path(d)
        a_mic, b_voice = (
            _track(rec_a, a_mic_track, tmp, "a_mic"),
            _track(rec_b, b_voice_track, tmp, "b_voice"),
        )
        ab = align_arrays(a_mic, b_voice, max_offset_s=max_offset_s)
        if b_mic_track is None or a_voice_track is None:
            return ab
        b_mic, a_voice = (
            _track(rec_b, b_mic_track, tmp, "b_mic"),
            _track(rec_a, a_voice_track, tmp, "a_voice"),
        )
        ba = align_arrays(b_mic, a_voice, max_offset_s=max_offset_s)
    return combine(ab, ba)
