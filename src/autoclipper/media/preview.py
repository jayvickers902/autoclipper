"""Still thumbnails and animated WebP previews."""

from __future__ import annotations

from pathlib import Path

from ._run import ffmpeg, prepare_output


def thumbnail(path: Path | str, t: float, out_jpg: Path | str, width: int = 640) -> Path:
    out = prepare_output(path, out_jpg)
    ffmpeg("-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-vf", f"scale={width}:-2",
           "-q:v", "3", "-update", "1", str(out))  # fmt: skip
    return out


def preview_webp(
    path: Path | str, start: float, out_webp: Path | str, seconds: float = 3, width: int = 480, fps: int = 12
) -> Path:
    out = prepare_output(path, out_webp)
    ffmpeg("-ss", f"{start:.3f}", "-i", str(path), "-t", f"{seconds:.3f}", "-an",
           "-vf", f"fps={fps},scale={width}:-2:flags=lanczos",
           "-c:v", "libwebp_anim", "-loop", "0", "-quality", "60", "-compression_level", "4",
           str(out))  # fmt: skip
    return out
