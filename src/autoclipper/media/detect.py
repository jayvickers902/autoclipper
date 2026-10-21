"""Dead-capture detection (all-black / frozen video) at reduced fps and scale."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ._run import FFMPEG, run
from .probe import probe

_BLACK = re.compile(r"black_start:([\d.]+)\s+black_end:([\d.]+)")
_FREEZE_START = re.compile(r"freeze_start:\s*([\d.]+)")
_FREEZE_END = re.compile(r"freeze_end:\s*([\d.]+)")


SAMPLE_FPS = 2
SAMPLE_WIDTH = 320


@dataclass(frozen=True)
class Interval:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def _scan(path: Path | str, vf: str, duration: float, start: float | None = None) -> str:
    """Run the filter over the whole file, or over `duration` seconds from `start` (input seek)."""
    window = [] if start is None else ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}"]
    proc = run(
        [FFMPEG, "-hide_banner", "-nostdin", "-nostats", *window, "-i", str(path), "-an", "-sn", "-dn",
         "-vf", f"fps={SAMPLE_FPS},scale={SAMPLE_WIDTH}:-2,{vf}", "-f", "null", "-"],
        timeout=max(120.0, duration * 4),
    )  # fmt: skip
    return proc.stderr


def _black_filter(min_duration: float, pix_th: float) -> str:
    return f"blackdetect=d={min_duration}:pix_th={pix_th}"


def _freeze_filter(min_duration: float, noise: float) -> str:
    return f"freezedetect=n={noise}:d={min_duration}"


def _parse_black(log: str) -> list[Interval]:
    return [Interval(float(a), float(b)) for a, b in _BLACK.findall(log)]


def _parse_freeze(log: str, duration: float) -> list[Interval]:
    starts = [float(x) for x in _FREEZE_START.findall(log)]
    ends = [float(x) for x in _FREEZE_END.findall(log)]
    # a freeze that runs to EOF may have no end line
    return [Interval(s, ends[i] if i < len(ends) else max(duration, s)) for i, s in enumerate(starts)]


def detect_black(path: Path | str, min_duration: float = 0.5, pix_th: float = 0.10) -> list[Interval]:
    """Black intervals of at least `min_duration` seconds."""
    return _parse_black(_scan(path, _black_filter(min_duration, pix_th), probe(path).duration))


def detect_frozen(path: Path | str, min_duration: float = 2.0, noise: float = 0.003) -> list[Interval]:
    """Intervals where the picture does not change (freezedetect) for at least `min_duration` seconds."""
    dur = probe(path).duration
    return _parse_freeze(_scan(path, _freeze_filter(min_duration, noise), dur), dur)


def is_dead_capture(path: Path | str, threshold: float = 0.9) -> tuple[bool, str]:
    """True when at least `threshold` of the clip is black or frozen. One decode pass, ~5 s per minute."""
    info = probe(path)
    if info.width == 0 or info.duration <= 0:
        return True, "no video stream" if info.width == 0 else "zero duration"
    log = _scan(path, f"{_black_filter(0.5, 0.10)},{_freeze_filter(2.0, 0.003)}", info.duration)
    black = sum(i.duration for i in _parse_black(log))
    frozen = sum(i.duration for i in _parse_freeze(log, info.duration))
    for label, seconds in (("black", black), ("frozen", frozen)):
        if seconds >= threshold * info.duration:
            return True, f"{label} for {seconds:.1f}s of {info.duration:.1f}s ({seconds / info.duration:.0%})"
    return False, f"ok (black {black:.1f}s, frozen {frozen:.1f}s of {info.duration:.1f}s)"


def is_dead_sampled(
    path: Path | str, samples: int = 4, seconds: float = 30.0, threshold: float = 0.9
) -> tuple[bool, str]:
    """is_dead_capture for long recordings: decode only `samples` windows of `seconds` spread evenly over
    the file (a full pass over hours of video costs minutes of CPU). Dead only when every window is dead."""
    info = probe(path)
    if info.width == 0 or info.duration <= 0:
        return True, "no video stream" if info.width == 0 else "zero duration"
    if info.duration <= samples * seconds:
        return is_dead_capture(path, threshold)
    step = (info.duration - seconds) / max(1, samples - 1)
    vf = f"{_black_filter(0.5, 0.10)},{_freeze_filter(2.0, 0.003)}"
    worst = []
    for i in range(samples):
        log = _scan(path, vf, seconds, start=i * step)
        black = sum(iv.duration for iv in _parse_black(log))
        frozen = sum(iv.duration for iv in _parse_freeze(log, seconds))
        share = max(black, frozen) / seconds
        if share < threshold:
            at = f"sample at {i * step:.0f}s"
            return False, f"ok ({at}: black {black:.1f}s, frozen {frozen:.1f}s of {seconds:.0f}s)"
        worst.append(share)
    return True, f"black or frozen in all {samples} samples of {seconds:.0f}s (min {min(worst):.0%})"
