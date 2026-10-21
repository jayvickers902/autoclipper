"""Multi-POV alignment on synthetic audio generated at test time (nothing binary is committed)."""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from autoclipper import align as al

SR = al.SR


def speech(seconds: float, seed: int) -> np.ndarray:
    """Speech-like float signal: phrases of voiced syllables (harmonics under random formants, with a
    syllable envelope) separated by pauses, plus the odd fricative."""
    rng = np.random.default_rng(seed)
    out = np.zeros(int(seconds * SR))
    t = rng.uniform(0.2, 1.0)
    while t < seconds - 0.5:
        for _ in range(rng.integers(3, 12)):  # one phrase
            dur = rng.uniform(0.08, 0.3)
            n = int(dur * SR)
            s0 = int(t * SR)
            if s0 + n >= len(out):
                break
            tt = np.arange(n) / SR
            f0 = rng.uniform(100, 210) * (1 + 0.05 * np.sin(2 * np.pi * rng.uniform(3, 6) * tt))
            phase = 2 * np.pi * np.cumsum(f0) / SR
            formants = rng.uniform([300, 900, 2200], [900, 2200, 3200])
            syl = np.zeros(n)
            for k in range(1, int(3800 / f0.mean())):
                fk = k * f0.mean()
                amp = sum(np.exp(-(((fk - f) / 150) ** 2)) for f in formants) + 0.02
                syl += amp * np.sin(k * phase)
            if rng.random() < 0.25:  # fricative
                syl += 0.5 * rng.standard_normal(n)
            out[s0 : s0 + n] += syl * np.hanning(n) * rng.uniform(0.3, 1.0)
            t += dur + rng.uniform(0.03, 0.25)
        t += rng.uniform(0.4, 2.5)
    return 0.3 * out / (np.abs(out).max() + 1e-9)


def game(seconds: float, seed: int) -> np.ndarray:
    """Gunfire-ish decaying noise bursts over an engine hum."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    tt = np.arange(n) / SR
    out = 0.05 * np.sin(2 * np.pi * 85 * tt) + 0.01 * rng.standard_normal(n)
    for start in rng.uniform(0, seconds - 1, int(seconds * 0.7)):
        m = int(rng.uniform(0.1, 0.5) * SR)
        s0 = int(start * SR)
        out[s0 : s0 + m] += 0.5 * rng.standard_normal(m) * np.exp(-np.arange(m) / (0.05 * SR))
    return out


def lowpass(x: np.ndarray, hz: float) -> np.ndarray:
    spec = np.fft.rfft(x)
    spec[np.fft.rfftfreq(len(x), 1 / SR) > hz] = 0
    return np.fft.irfft(spec, len(x))


def discord(x: np.ndarray, seed: int) -> np.ndarray:
    """Opus/Discord-ish damage: 8 kHz round trip by linear interpolation, low-pass, gain, soft clip, hiss."""
    rng = np.random.default_rng(seed)
    t = np.arange(len(x))
    coarse = lowpass(x, 3800)[::2]
    y = np.interp(t, t[::2], coarse)
    y = np.tanh(3 * 0.4 * y) / 3
    return y + 0.003 * rng.standard_normal(len(y))


def shift(x: np.ndarray, seconds: float, length: int) -> np.ndarray:
    """x moved later by `seconds` (earlier if negative), cut/padded to `length` samples."""
    out = np.zeros(length)
    k = int(round(seconds * SR))
    src = x[max(0, -k) :]
    dst0 = max(0, k)
    m = min(len(src), length - dst0)
    if m > 0:
        out[dst0 : dst0 + m] = src[:m]
    return out


def scene(offset: float, latency: float = 0.0, seconds: float = 60.0, seed: int = 0):
    """A and B recordings of the same session. offset = t_b - t_a. Returns per-track float arrays."""
    va, vb, vc = speech(seconds, seed + 1), speech(seconds, seed + 2), speech(seconds, seed + 3)
    n_b = int((seconds - 5) * SR)
    a_mic = va + 0.002 * np.random.default_rng(seed + 4).standard_normal(len(va))
    b_mic = shift(vb, offset, n_b)
    # B's voice-chat track: A (and friend C) via Discord, plus B's game audio bleeding in.
    b_voice = shift(discord(va + vc, seed + 5), offset + latency, n_b) + 0.5 * game(n_b / SR, seed + 6)
    # A's voice-chat track: B (and C) via Discord, latency later in A's timeline.
    a_voice = discord(shift(vb, latency, len(va)) + vc, seed + 7) + 0.5 * game(seconds, seed + 8)
    return a_mic, b_voice, b_mic, a_voice


@pytest.mark.parametrize("offset", [1.37, -2.84, 0.0, -12.5, 20.0])
def test_offset_recovered_through_discord_damage(offset: float) -> None:
    a_mic, b_voice, *_ = scene(offset, seed=int(abs(offset) * 100))
    r = al.align_arrays(a_mic, b_voice, max_offset_s=60)
    assert r.offset_s == pytest.approx(offset, abs=0.02), r
    assert r.confidence >= 0.8, r


def test_int16_input_and_gain_invariance() -> None:
    a_mic, b_voice, *_ = scene(1.37, seed=3)
    a16 = (np.clip(a_mic * 0.1, -1, 1) * 32767).astype(np.int16)
    b16 = (np.clip(b_voice * 2.0, -1, 1) * 32767).astype(np.int16)
    r = al.align_arrays(a16, b16, max_offset_s=30)
    assert r.offset_s == pytest.approx(1.37, abs=0.02) and r.confidence >= 0.8, r


@pytest.mark.parametrize("seed", [10, 20, 30])
def test_unrelated_audio_is_low_confidence(seed: int) -> None:
    a_mic = speech(60, seed)
    b_voice = discord(speech(55, seed + 1) + speech(55, seed + 2), seed) + 0.5 * game(55, seed + 3)
    r = al.align_arrays(a_mic, b_voice, max_offset_s=60)
    assert r.confidence <= 0.2, r


def test_noise_and_silence_are_low_confidence() -> None:
    rng = np.random.default_rng(5)
    assert al.align_arrays(rng.standard_normal(SR * 30), rng.standard_normal(SR * 30)).confidence <= 0.2
    silent = al.align_arrays(np.zeros(SR * 30), speech(30, 1))
    assert silent.confidence == 0 and silent.method == "none"


def test_both_directions_cancel_latency() -> None:
    offset, latency = 1.37, 0.15
    a_mic, b_voice, b_mic, a_voice = scene(offset, latency, seed=7)
    ab = al.align_arrays(a_mic, b_voice, max_offset_s=30)
    ba = al.align_arrays(b_mic, a_voice, max_offset_s=30)
    assert ab.offset_s == pytest.approx(offset + latency, abs=0.02)  # one direction carries the latency
    r = al.combine(ab, ba)
    assert r.method == "both-directions"
    assert r.offset_s == pytest.approx(offset, abs=0.02), r
    assert r.details["latency_s"] == pytest.approx(latency, abs=0.02)
    assert r.confidence >= 0.9


def test_combine_falls_back_to_the_confident_direction() -> None:
    good = al.Alignment(2.0, 0.9, "fine")
    r = al.combine(good, al.Alignment(0.0, 0.0, "coarse"))
    assert r.offset_s == 2.0 and r.confidence == 0.9 and "one direction" in r.details["note"]
    r = al.combine(al.Alignment(0.0, 0.0, "coarse"), good)
    assert r.offset_s == -2.0


def test_long_recording_large_offset() -> None:
    """Ten minutes against a B that started 3m25s later, A talking only now and then."""
    seconds, offset = 600.0, -205.31
    rng = np.random.default_rng(11)
    va = np.zeros(int(seconds * SR))
    for start in rng.uniform(0, seconds - 20, 12):  # 12 bursts of chatter
        s0 = int(start * SR)
        va[s0 : s0 + 15 * SR] += speech(15, int(start))
    n_b = int(450 * SR)
    b_voice = shift(discord(va + speech(seconds, 99), 1), offset, n_b) + 0.5 * game(450, 2)
    r = al.align_arrays(va, b_voice, max_offset_s=300)
    assert r.offset_s == pytest.approx(offset, abs=0.02), r
    assert r.confidence >= 0.8


def _write_wav(path: Path, x: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def _mux(out: Path, wavs: list[Path], codecs: list[str]) -> Path:
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for w in wavs:
        cmd += ["-i", str(w)]
    for i in range(len(wavs)):
        cmd += ["-map", f"{i}:a"]
    for i, c in enumerate(codecs):
        cmd += [f"-c:a:{i}", c, f"-ar:a:{i}", "48000"]
        if c == "libopus":  # Discord voice runs at 64 kbps by default; be harsher
            cmd += [f"-b:a:{i}", "32k"]
    subprocess.run([*cmd, str(out)], check=True, stdin=subprocess.DEVNULL, capture_output=True)
    return out


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_align_container_files(tmp_path: Path) -> None:
    """A: OBS-layout mp4 (AAC). B: Segra-layout mka with real Opus on the voice-chat track."""
    offset, latency = 1.37, 0.1
    a_mic, b_voice, b_mic, a_voice = scene(offset, latency, seconds=40, seed=21)
    a_game, b_game = 0.5 * game(40, 31), 0.5 * game(35, 32)
    w = {name: _write_wav(tmp_path / f"{name}.wav", x) for name, x in {
        "a_mix": a_mic + a_voice[: len(a_mic)] + a_game, "a_game": a_game, "a_voice": a_voice, "a_mic": a_mic,
        "b_mix": b_mic + b_voice + b_game, "b_mic": b_mic, "b_game": b_game, "b_voice": b_voice,
    }.items()}  # fmt: skip
    rec_a = _mux(tmp_path / "a.mp4", [w["a_mix"], w["a_game"], w["a_voice"], w["a_mic"]], ["aac"] * 4)
    rec_b = _mux(tmp_path / "b.mka", [w["b_mix"], w["b_mic"], w["b_game"], w["b_voice"]],
                 ["aac", "aac", "aac", "libopus"])  # fmt: skip
    a_t, b_t = al.OBS_TRACKS, al.SEGRA_TRACKS
    one = al.align(rec_a, rec_b, a_mic_track=a_t["mic"], b_voice_track=b_t["voice"], max_offset_s=30)
    assert one.offset_s == pytest.approx(offset + latency, abs=0.03) and one.confidence >= 0.8, one
    both = al.align(rec_a, rec_b, a_mic_track=a_t["mic"], b_voice_track=b_t["voice"],
                    b_mic_track=b_t["mic"], a_voice_track=a_t["voice"], max_offset_s=30)  # fmt: skip
    assert both.method == "both-directions"
    assert both.offset_s == pytest.approx(offset, abs=0.02), both
    assert both.confidence >= 0.9
    with pytest.raises(al.MediaError):
        al.align(rec_a, rec_b, a_mic_track=9, b_voice_track=4)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_align_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`autoclipper align A B`: OBS layout against Segra layout, both directions, text and JSON."""
    import json

    from autoclipper.cli import main

    offset, latency = 1.37, 0.1
    a_mic, b_voice, b_mic, a_voice = scene(offset, latency, seconds=40, seed=21)
    g = 0.5 * game(40, 31)
    w = {name: _write_wav(tmp_path / f"{name}.wav", x) for name, x in {
        "mix": g, "game": g, "a_voice": a_voice, "a_mic": a_mic,
        "b_mic": b_mic, "b_game": g[: len(b_mic)], "b_voice": b_voice,
    }.items()}  # fmt: skip
    rec_a = _mux(tmp_path / "a.mp4", [w["mix"], w["game"], w["a_voice"], w["a_mic"]], ["aac"] * 4)
    rec_b = _mux(tmp_path / "b.mka", [w["mix"], w["b_mic"], w["b_game"], w["b_voice"]], ["aac"] * 4)
    assert main(["align", str(rec_a), str(rec_b), "--max-offset", "30"]) == 0
    out = capsys.readouterr().out
    assert "offset (t_b - t_a): +1.3" in out and "both-directions" in out
    assert main(["align", str(rec_a), str(rec_b), "--a-layout", "obs", "--b-layout", "segra", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["offset_s"] == pytest.approx(offset, abs=0.03) and data["confidence"] >= 0.8
    assert data["latency_s"] == pytest.approx(latency, abs=0.03)
    # wrong layout for B: track 4 exists on both, so ask for more tracks than there are instead
    assert main(["align", str(tmp_path / "missing.mp4"), str(rec_b)]) == 2
