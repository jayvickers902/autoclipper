"""Share-sized H.264 encode (NVENC when it really works, else libx264)."""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

from ._run import FFMPEG, MediaError, ffmpeg, prepare_output, run
from .probe import probe

AUDIO_KBPS = 128
MB = 1_000_000  # decimal, so "19 MB" is conservative against a 25 MiB limit
MIN_VIDEO_KBPS = 100


@dataclass(frozen=True)
class EncodeResult:
    path: Path
    size_bytes: int
    encoder: str
    video_kbps: int
    attempts: int


@functools.cache
def nvenc_works() -> bool:
    """True if h264_nvenc is listed AND a 1-frame test encode succeeds (cached per process)."""
    try:
        if "h264_nvenc" not in run([FFMPEG, "-hide_banner", "-encoders"]).stdout:
            return False
        run([FFMPEG, "-hide_banner", "-nostdin", "-f", "lavfi", "-i", "color=c=black:s=640x360:d=0.1",
             "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-"], timeout=30)  # fmt: skip
    except MediaError:
        return False
    return True


def _video_args(encoder: str, kbps: int) -> list[str]:
    rate = ["-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k"]
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", *rate]
    return ["-c:v", "libx264", "-preset", "veryfast", *rate]


def share_encode(
    path: Path | str,
    start: float,
    end: float,
    out: Path | str,
    max_mb: float = 19.0,
    encoder: str = "auto",
) -> EncodeResult:
    """Encode [start, end] to <= 1080p, <= 60 fps H.264 MP4 (first audio track as AAC) of at most max_mb MB.

    `encoder` is "auto" (nvenc if it works), "libx264" or "h264_nvenc". Retries once at a lower bitrate if the
    first result is over budget; raises MediaError if still too big.
    """
    dest = prepare_output(path, out)
    info = probe(path)
    end = min(end, info.duration) if info.duration else end
    dur = end - start
    if dur <= 0:
        raise MediaError(f"end {end} must be after start {start}")
    if encoder == "auto":
        encoder = "h264_nvenc" if nvenc_works() else "libx264"
    max_bytes = int(max_mb * MB)
    has_audio = bool(info.audio)
    audio_kbps = min(AUDIO_KBPS, max(32, int(max_bytes * 8 / dur / 1000 * 0.15))) if has_audio else 0
    # 6% container/VBV headroom
    kbps = max(MIN_VIDEO_KBPS, int(max_bytes * 8 * 0.94 / dur / 1000) - audio_kbps)

    vf = "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
    if info.fps > 60.5:
        vf = f"fps=60,{vf}"
    vf += ",format=yuv420p"

    size = 0
    for attempt in (1, 2):
        args = ["-ss", f"{start:.3f}", "-i", str(path), "-t", f"{dur:.3f}", "-map", "0:v:0"]
        if has_audio:
            args += ["-map", "0:a:0"]
        args += ["-vf", vf, *_video_args(encoder, kbps)]
        args += ["-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ac", "2"] if has_audio else ["-an"]
        args += ["-movflags", "+faststart", str(dest)]
        ffmpeg(*args)
        size = dest.stat().st_size
        if size <= max_bytes:
            return EncodeResult(dest, size, encoder, kbps, attempt)
        kbps = max(MIN_VIDEO_KBPS, int(kbps * max_bytes / size * 0.9))
    dest.unlink(missing_ok=True)
    raise MediaError(f"encode is {size / MB:.1f} MB, over the {max_mb} MB budget")
