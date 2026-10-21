"""Finding replay files: top-level `Replay *.mp4` only, never the output dir, only once the size is stable."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterable
from pathlib import Path

PATTERN = "Replay *.mp4"
CHUNK = 1 << 20

Signature = tuple[int, int]  # (size, mtime_ns)


def candidate_files(directory: Path, output_dir: Path) -> list[Path]:
    """Top-level `Replay *.mp4` files of `directory` (not recursive), oldest name first, minus output_dir."""
    out = output_dir.resolve(strict=False)
    files = []
    for p in sorted(directory.glob(PATTERN)):
        if p.is_file() and not p.resolve().is_relative_to(out):
            files.append(p)
    return files


def signature(path: Path) -> Signature | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


class StabilityTracker:
    """A file is stable once two observations (e.g. two watch passes) see the same non-zero size and mtime."""

    def __init__(self) -> None:
        self._seen: dict[Path, Signature] = {}

    def observe(self, paths: Iterable[Path]) -> list[Path]:
        stable = []
        for p in paths:
            sig = signature(p)
            if sig is None:
                self._seen.pop(p, None)
                continue
            if sig[0] > 0 and self._seen.get(p) == sig:
                stable.append(p)
            self._seen[p] = sig
        return stable


def settle(paths: Iterable[Path], wait: float, sleep: Callable[[float], None] = time.sleep) -> list[Path]:
    """Two checks `wait` seconds apart; returns the files whose size and mtime did not change."""
    paths = list(paths)
    tracker = StabilityTracker()
    tracker.observe(paths)
    if paths:
        sleep(wait)
    return tracker.observe(paths)


def file_sha256(path: Path) -> str:
    """Full SHA-256, read-only, 1 MiB at a time."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()
