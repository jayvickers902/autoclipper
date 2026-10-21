"""Generate small synthetic media fixtures for the test suite.

    uv run python tests/fixtures/make_fixtures.py [outdir] [--force]

Default outdir is tests/fixtures/generated/ (gitignored). Existing files are skipped unless --force.
Needs ffmpeg and espeak-ng on PATH. Audio is synthesised with the stdlib (exact timing), then muxed with
the video by ffmpeg. All videos are 640x360, 30 fps, H.264 + AAC in .mp4 with a 1 s keyframe interval.

Each clip with a known ground truth gets a sibling .json (burst times, speech start times in seconds).
"""

from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import subprocess
import sys
import tempfile
import wave
from array import array
from pathlib import Path

RATE = 48_000
SIZE = "640x360"
FPS = 30
DEFAULT_OUT = Path(__file__).resolve().parent / "generated"

Samples = array  # array('h'), mono int16 at RATE


def silence(seconds: float) -> Samples:
    return array("h", bytes(2 * round(seconds * RATE)))


def noise(seconds: float, amp: float, seed: int) -> Samples:
    rng = random.Random(seed)
    peak = int(amp * 32767)
    return array("h", (rng.randint(-peak, peak) for _ in range(round(seconds * RATE))))


def tone(seconds: float, freq: float, amp: float) -> Samples:
    peak = amp * 32767
    return array(
        "h", (int(peak * math.sin(2 * math.pi * freq * i / RATE)) for i in range(round(seconds * RATE)))
    )


def add_burst(buf: Samples, start_s: float, dur_s: float = 0.05, amp: float = 0.9, seed: int = 0) -> None:
    """Gunshot-like transient: a decaying noise burst of dur_s starting at start_s."""
    rng = random.Random(seed)
    start = round(start_s * RATE)
    n = round(dur_s * RATE)
    for i in range(n):
        if start + i >= len(buf):
            break
        env = math.exp(-5.0 * i / n)
        v = buf[start + i] + int(amp * 32767 * env * rng.uniform(-1, 1))
        buf[start + i] = max(-32768, min(32767, v))


def add_samples(buf: Samples, start_s: float, src: Samples) -> None:
    start = round(start_s * RATE)
    for i, s in enumerate(src):
        if start + i >= len(buf):
            break
        buf[start + i] = max(-32768, min(32767, buf[start + i] + s))


def mix(*tracks: Samples) -> Samples:
    out = array("h", bytes(2 * max(len(t) for t in tracks)))
    for t in tracks:
        for i, s in enumerate(t):
            out[i] = max(-32768, min(32767, out[i] + s))
    return out


def write_wav(path: Path, samples: Samples) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())


def run(*cmd: str) -> None:
    subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL, capture_output=True)


def speak(text: str, tmp: Path) -> tuple[Samples, float]:
    """espeak-ng `text` at RATE with leading silence trimmed. Returns (samples, duration_s)."""
    raw, conv = tmp / "say.wav", tmp / "say48k.wav"
    run("espeak-ng", "-v", "en-us", "-s", "150", "-w", str(raw), text)
    run("ffmpeg", "-y", "-i", str(raw), "-ar", str(RATE), "-ac", "1", "-sample_fmt", "s16", str(conv))
    with wave.open(str(conv), "rb") as w:
        pcm = array("h")
        pcm.frombytes(w.readframes(w.getnframes()))
    first = next((i for i, s in enumerate(pcm) if abs(s) > 300), 0)
    pcm = pcm[first:]
    return pcm, len(pcm) / RATE


def encode(out: Path, video_args: list[str], audio_wavs: list[Path], titles: list[str] | None = None) -> None:
    """Mux a lavfi/png video input with one AAC stream per wav; written atomically."""
    part = out.with_name(out.stem + ".part.mp4")
    cmd = ["ffmpeg", "-y", *video_args]
    for wav in audio_wavs:
        cmd += ["-i", str(wav)]
    cmd += ["-map", "0:v"]
    for i in range(len(audio_wavs)):
        cmd += ["-map", f"{i + 1}:a"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
        "-g", str(FPS), "-keyint_min", str(FPS), "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", "64k", "-shortest",
    ]  # fmt: skip
    for i, title in enumerate(titles or []):
        cmd += [f"-metadata:s:a:{i}", f"handler_name={title}"]
    run(*cmd, str(part))
    part.replace(out)


def lavfi(src: str) -> list[str]:
    return ["-f", "lavfi", "-i", src]


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---- individual fixtures: each takes (out_dir, tmp) and writes <name>.mp4 (+ <name>.json) ----


def make_black(out: Path, tmp: Path) -> None:
    wav = tmp / "black.wav"
    write_wav(wav, noise(10, 0.002, seed=1))
    encode(out / "black.mp4", lavfi(f"color=c=black:s={SIZE}:r={FPS}:d=10"), [wav])


def make_static(out: Path, tmp: Path) -> None:
    frame, wav = tmp / "frame.png", tmp / "static.wav"
    run("ffmpeg", "-y", *lavfi(f"testsrc=s={SIZE}:r={FPS}"), "-frames:v", "1", str(frame))
    write_wav(wav, noise(10, 0.01, seed=2))
    encode(out / "static.mp4", ["-loop", "1", "-framerate", str(FPS), "-t", "10", "-i", str(frame)], [wav])


FIREFIGHT_BURSTS = [6.0, 6.4, 7.1, 7.3, 12.0, 12.2]


def make_firefight(out: Path, tmp: Path) -> None:
    wav = tmp / "firefight.wav"
    buf = noise(20, 0.01, seed=3)
    for i, t in enumerate(FIREFIGHT_BURSTS):
        add_burst(buf, t, seed=100 + i)
    write_wav(wav, buf)
    encode(out / "firefight.mp4", lavfi(f"testsrc2=s={SIZE}:r={FPS}:d=20"), [wav])
    write_json(
        out / "firefight.json", {"duration_s": 20.0, "burst_times_s": FIREFIGHT_BURSTS, "burst_len_s": 0.05}
    )


def make_speech(out: Path, tmp: Path) -> None:
    wav = tmp / "speech.wav"
    buf = noise(8, 0.005, seed=4)
    truth = []
    for text, start in (("clip that", 3.0), ("nice shot", 6.0)):
        pcm, dur = speak(text, tmp)
        add_samples(buf, start, pcm)
        truth.append({"text": text, "start_s": start, "end_s": round(start + dur, 3)})
    write_wav(wav, buf)
    encode(out / "speech.mp4", lavfi(f"testsrc2=s={SIZE}:r={FPS}:d=8"), [wav])
    write_json(out / "speech.json", {"duration_s": 8.0, "phrases": truth})


FOUR_BURSTS = [2.0, 2.4, 6.5, 6.7]
FOUR_SPEECH = ("clip that", 4.0)


def make_four_tracks(out: Path, tmp: Path) -> None:
    game = noise(10, 0.004, seed=5)
    for i, t in enumerate(FOUR_BURSTS):
        add_burst(game, t, seed=200 + i)
    discord = silence(10)
    pcm, dur = speak(FOUR_SPEECH[0], tmp)
    add_samples(discord, FOUR_SPEECH[1], pcm)
    micro = tone(10, 440.0, 0.2)
    tracks = [mix(game, discord, micro), game, discord, micro]
    wavs = []
    for n, samples in enumerate(tracks, start=1):
        wavs.append(tmp / f"track{n}.wav")
        write_wav(wavs[-1], samples)
    titles = ["mix", "game", "discord", "mic"]
    encode(out / "four_tracks.mp4", lavfi(f"testsrc2=s={SIZE}:r={FPS}:d=10"), wavs, titles)
    write_json(out / "four_tracks.json", {
        "duration_s": 10.0,
        "tracks": {"1": "mix of 2-4", "2": "game: noise bed plus bursts", "3": "discord: speech",
                   "4": "mic: 440 Hz sine, amplitude 0.2, whole clip"},
        "burst_times_s": FOUR_BURSTS,
        "burst_track": 2,
        "speech": {"track": 3, "text": FOUR_SPEECH[0], "start_s": FOUR_SPEECH[1],
                   "end_s": round(FOUR_SPEECH[1] + dur, 3)},
    })  # fmt: skip


def make_long(
    out: Path,
    duration: float,
    bursts: list[float],
    chapters: list[tuple[float, str]] | None = None,
    seed: int = 7,
) -> Path:
    """A long, small recording for ingest tests (not part of FIXTURES; tests build it in a tmp dir).

    160x90 testsrc2 at 10 fps (libx264 ultrafast, 1 s keyframes), one 16 kHz AAC track: a quiet noise bed
    plus a gunshot-like burst at each time in `bursts`; `chapters` [(t, title)] become MP4 chapters (as an
    OBS chapter marker or a Segra bookmark would). numpy keeps a 10-minute track fast to synthesise.
    """
    import numpy as np

    rate = 16_000
    rng = np.random.default_rng(seed)
    pcm = rng.uniform(-0.01, 0.01, int(duration * rate))
    n = int(0.05 * rate)
    env = np.exp(-5.0 * np.arange(n) / n)
    for t in bursts:
        a = int(t * rate)
        seg = pcm[a : a + n]
        seg += 0.9 * env[: seg.size] * rng.uniform(-1, 1, seg.size)
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wav = tmp / "long.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
        meta_args: list[str] = []
        if chapters:
            meta = tmp / "chapters.txt"
            lines = [";FFMETADATA1"]
            marks = sorted(chapters) + [(duration, "")]
            for (start, title), (end, _) in zip(marks, marks[1:], strict=False):
                lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start * 1000)}",
                          f"END={int(end * 1000)}", f"title={title}"]  # fmt: skip
            meta.write_text("\n".join(lines) + "\n", encoding="utf-8")
            meta_args = ["-i", str(meta), "-map_chapters", "2"]
        part = out.with_name(out.stem + ".part" + out.suffix)
        run(
            "ffmpeg", "-y", *lavfi(f"testsrc2=s=160x90:r=10:d={duration}"), "-i", str(wav), *meta_args,
            "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35",
            "-pix_fmt", "yuv420p", "-g", "10", "-keyint_min", "10", "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", "32k", "-t", str(duration), str(part),
        )  # fmt: skip
        part.replace(out)
    return out


FIXTURES = {
    "black": (make_black, ["black.mp4"]),
    "static": (make_static, ["static.mp4"]),
    "firefight": (make_firefight, ["firefight.mp4", "firefight.json"]),
    "speech": (make_speech, ["speech.mp4", "speech.json"]),
    "four_tracks": (make_four_tracks, ["four_tracks.mp4", "four_tracks.json"]),
}


def generate(out: Path, force: bool = False) -> list[str]:
    """Create missing fixtures in `out`; returns the names generated."""
    for tool in ("ffmpeg", "espeak-ng"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"{tool} not found on PATH")
    out.mkdir(parents=True, exist_ok=True)
    made = []
    for name, (fn, files) in FIXTURES.items():
        if not force and all((out / f).exists() for f in files):
            continue
        with tempfile.TemporaryDirectory() as t:
            fn(out, Path(t))
        made.append(name)
    return made


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("outdir", nargs="?", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--force", action="store_true", help="regenerate files that already exist")
    args = ap.parse_args(argv)
    made = generate(args.outdir, args.force)
    print(f"generated {made or 'nothing (all present)'} in {args.outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
