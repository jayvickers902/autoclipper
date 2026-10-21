"""Per-second loudness and transient (onset) detection on 16 kHz mono audio (numpy only)."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

FLOOR_DB = -90.0  # digital silence is reported as this, never -inf
HOP_S = 0.010  # event time resolution
REFRACTORY_S = 0.040
JUMP_DB = 12.0  # a frame must be this much louder than the quietest of the previous 30 ms ...
BASELINE_S = 0.5  # ... and than the median of the preceding half second
BASELINE_DB = 10.0
DECAY_DB = 10.0  # ... and fall this far below its peak within 30-80 ms (gunshots do, speech onsets do not)
MIN_DB = -45.0  # (pre-emphasised) transients quieter than this are noise, not events
STRENGTH_SPAN_DB = 40.0  # a transient this far above its baseline has strength 1.0


def read_wav(path: Path | str) -> tuple[np.ndarray, int]:
    """Mono s16 WAV -> (float32 samples in [-1, 1], sample rate)."""
    with wave.open(str(path), "rb") as w:
        sr, n = w.getframerate(), w.getnframes()
        pcm = np.frombuffer(w.readframes(n), dtype="<i2")
    return pcm.astype(np.float32) / 32768.0, sr


def to_db(x: np.ndarray | float) -> np.ndarray:
    return np.maximum(20.0 * np.log10(np.maximum(x, 1e-9)), FLOOR_DB)


def per_second_levels(samples: np.ndarray, sr: int, n_seconds: int) -> tuple[np.ndarray, np.ndarray]:
    """RMS and peak dBFS for each whole second 0..n_seconds-1 (the last one may be partial)."""
    rms, peak = np.full(n_seconds, FLOOR_DB), np.full(n_seconds, FLOOR_DB)
    for s in range(n_seconds):
        chunk = samples[s * sr : (s + 1) * sr]
        if chunk.size:
            rms[s] = to_db(float(np.sqrt(np.mean(np.square(chunk, dtype=np.float64)))))
            peak[s] = to_db(float(np.max(np.abs(chunk))))
    return rms, peak


def detect_transients(samples: np.ndarray, sr: int) -> list[tuple[float, float]]:
    """Sharp broadband energy jumps as (time_s, strength 0..1), ~10 ms resolution.

    Pre-emphasis (first difference) favours broadband clicks and gunshots over voice and hum; energy is
    measured per 10 ms frame in dB. A frame is a transient when it jumps `JUMP_DB` over the loudest of the
    previous three frames and `BASELINE_DB` over the median of the preceding `BASELINE_S`, clears `MIN_DB`,
    and decays by `DECAY_DB` within 80 ms (so the start of a word or a tone is not a transient).
    Hits closer than the refractory period are merged (the stronger one wins).
    """
    hop = int(sr * HOP_S)
    n = samples.size // hop
    if n < 8:
        return []
    emph = np.diff(samples, prepend=samples[:1]).astype(np.float64)
    frames = emph[: n * hop].reshape(n, hop)
    e = to_db(np.sqrt(np.mean(frames**2, axis=1)))
    nb = int(BASELINE_S / HOP_S)
    pad = np.concatenate([np.full(nb + 3, FLOOR_DB), e])  # pad[k + nb + 3] == e[k]
    baseline = np.median(np.lib.stride_tricks.sliding_window_view(pad, nb)[:n], axis=1)  # e[i-nb-3 .. i-4]
    prev = np.maximum.reduce([pad[nb + k : nb + k + n] for k in (0, 1, 2)])  # e[i-3 .. i-1]
    hits = (e - prev > JUMP_DB) & (e - baseline > BASELINE_DB) & (e > MIN_DB)
    after = np.concatenate([e, np.full(8, e[-1])])
    out: list[tuple[float, float]] = []
    refractory = int(REFRACTORY_S / HOP_S)
    last = -refractory
    for i in np.flatnonzero(hits):
        top = max(e[i], after[i + 1])
        if after[i + 3 : i + 8].min() > top - DECAY_DB:
            continue
        strength = float(np.clip((top - baseline[i]) / STRENGTH_SPAN_DB, 0.0, 1.0))
        if i - last < refractory and out:
            if strength > out[-1][1]:
                out[-1] = (out[-1][0], round(strength, 3))
            continue
        out.append((round(float(i * HOP_S), 3), round(strength, 3)))
        last = int(i)
    return out


def onset_counts(transients: list[tuple[float, float]], n_seconds: int) -> list[int]:
    counts = [0] * n_seconds
    for t, _ in transients:
        s = int(t)
        if 0 <= s < n_seconds:
            counts[s] += 1
    return counts
