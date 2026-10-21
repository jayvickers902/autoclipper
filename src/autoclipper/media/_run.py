"""Subprocess helpers shared by the media primitives."""

from __future__ import annotations

import subprocess
from pathlib import Path

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


class MediaError(RuntimeError):
    """ffmpeg/ffprobe failed or an unsafe request was refused."""


def run(args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing text output; raise MediaError on failure."""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, errors="replace", timeout=timeout, stdin=subprocess.DEVNULL
        )
    except FileNotFoundError as e:
        raise MediaError(f"{args[0]} not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise MediaError(f"{args[0]} timed out after {timeout}s") from e
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
        raise MediaError(f"{args[0]} exited {proc.returncode}: {tail}")
    return proc


def ffmpeg(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run ffmpeg quietly (errors only unless `-loglevel` is given afterwards)."""
    return run([FFMPEG, "-hide_banner", "-nostdin", "-nostats", "-y", *args], timeout=timeout)


def prepare_output(src: Path | str, out: Path | str) -> Path:
    """Resolve `out`, refuse to overwrite the input, and create its parent directory."""
    src_p, out_p = Path(src).resolve(), Path(out).resolve()
    if out_p == src_p or (out_p.exists() and src_p.exists() and out_p.samefile(src_p)):
        raise MediaError(f"refusing to overwrite the input file: {src_p}")
    out_p.parent.mkdir(parents=True, exist_ok=True)
    return out_p
