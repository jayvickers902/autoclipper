"""Keyframe-aware stream-copy cutting."""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from pathlib import Path

from ._run import MediaError, ffmpeg, prepare_output
from .probe import keyframe_times, probe


@dataclass(frozen=True)
class CutResult:
    path: Path
    start: float  # actual start used (a keyframe at or before the requested start)
    end: float


def cut_copy(path: Path | str, start: float, end: float, out: Path | str) -> CutResult:
    """Copy [start, end] without re-encoding, snapping start back to the previous keyframe."""
    dest = prepare_output(path, out)
    duration = probe(path).duration
    if end <= start:
        raise MediaError(f"end {end} must be after start {start}")
    end = min(end, duration)
    keys = keyframe_times(path)
    i = bisect.bisect_right(keys, start + 1e-3) - 1
    actual_start = keys[max(i, 0)]
    if end <= actual_start:
        raise MediaError(f"range {start}-{end} lies past the last keyframe/duration")
    ffmpeg("-ss", f"{actual_start:.3f}", "-i", str(path), "-t", f"{end - actual_start:.3f}",
           "-map", "0", "-dn", "-c", "copy", "-avoid_negative_ts", "make_zero", str(dest))  # fmt: skip
    return CutResult(dest, actual_start, end)
