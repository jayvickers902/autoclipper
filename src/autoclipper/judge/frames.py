"""Frames for the judge: 8-12 JPEGs from the candidate window, longest side <= 1024 px, as base64."""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path

from ..media import MediaError
from ..media._run import ffmpeg

MIN_FRAMES, MAX_FRAMES = 8, 12
MAX_PX = 1024
# Longest side <= MAX_PX, never upscaled, even dimensions.
SCALE = f"scale='if(gte(iw,ih),min({MAX_PX},iw),-2)':'if(gte(iw,ih),-2,min({MAX_PX},ih))'"


def frame_times(start: float, end: float, n: int | None = None) -> list[float]:
    """Evenly spaced sample times inside [start, end]: one per ~4 s, clamped to 8-12."""
    span = max(end - start, 0.1)
    n = n or min(MAX_FRAMES, max(MIN_FRAMES, round(span / 4)))
    return [round(start + (i + 0.5) * span / n, 2) for i in range(n)]


def extract_frames(path: Path | str, times: list[float], quality: int = 4) -> list[tuple[float, str]]:
    """(t, base64 JPEG) for each time that decodes; MediaError if none do. The replay is only read."""
    out: list[tuple[float, str]] = []
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ac-frames-") as tmp:
        for i, t in enumerate(times):
            jpg = Path(tmp) / f"f{i:02d}.jpg"
            try:
                ffmpeg("-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-vf", SCALE,
                       "-q:v", str(quality), "-update", "1", str(jpg), timeout=60)  # fmt: skip
            except MediaError as e:
                errors.append(f"{t}: {e}")
                continue
            if jpg.is_file() and jpg.stat().st_size:
                out.append((t, base64.b64encode(jpg.read_bytes()).decode("ascii")))
    if not out:
        raise MediaError(f"no frames decoded from {path}: {'; '.join(errors)[:300]}")
    return out


def image_block(b64: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}
