"""ffprobe wrappers: stream/format info and keyframe positions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from ._run import FFPROBE, MediaError, run


@dataclass(frozen=True)
class AudioStream:
    index: int  # absolute ffprobe stream index (what extract_audio takes)
    order: int  # 0-based position among the audio streams
    codec: str
    channels: int
    sample_rate: int
    title: str  # tags.title, else handler_name, else ""


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    video_codec: str
    audio: list[AudioStream] = field(default_factory=list)
    size_bytes: int = 0
    bitrate: int = 0  # bits/s, whole container


def _fps(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            f = Fraction(stream.get(key) or "0/0")
        except (ZeroDivisionError, ValueError):
            continue
        if f > 0:
            return float(f)
    return 0.0


def probe(path: Path | str) -> MediaInfo:
    p = Path(path)
    proc = run([FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(p)])
    data = json.loads(proc.stdout)
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = [
        AudioStream(
            index=int(s["index"]),
            order=i,
            codec=s.get("codec_name", ""),
            channels=int(s.get("channels", 0)),
            sample_rate=int(s.get("sample_rate", 0)),
            title=(s.get("tags", {}).get("title") or s.get("tags", {}).get("handler_name") or ""),
        )
        for i, s in enumerate(s for s in streams if s.get("codec_type") == "audio")
    ]
    duration = float(fmt.get("duration") or (video or {}).get("duration") or 0.0)
    return MediaInfo(
        path=p,
        duration=duration,
        width=int((video or {}).get("width", 0)),
        height=int((video or {}).get("height", 0)),
        fps=_fps(video) if video else 0.0,
        video_codec=(video or {}).get("codec_name", ""),
        audio=audio,
        size_bytes=int(fmt.get("size") or p.stat().st_size),
        bitrate=int(fmt.get("bit_rate") or 0),
    )


def keyframe_times(path: Path | str) -> list[float]:
    """Sorted presentation times (s) of the first video stream's keyframes, from packet flags (no decode)."""
    proc = run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags",
         "-of", "csv=p=0", str(path)]
    )  # fmt: skip
    times: list[float] = []
    for line in proc.stdout.splitlines():
        t, _, flags = line.partition(",")
        if "K" in flags and t not in ("", "N/A"):
            times.append(float(t))
    if not times:
        raise MediaError(f"no keyframes found in {path}")
    return sorted(times)
