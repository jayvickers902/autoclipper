"""The one write gate: autoclipper writes only under output_dir and data_dir; originals are read-only."""

from __future__ import annotations

from pathlib import Path

from .config import Settings


class WriteRefused(PermissionError):
    """A write outside output_dir/data_dir (or into the replay directory) was refused."""


def assert_writable(path: Path | str, settings: Settings) -> Path:
    """Return `path` resolved if it lies under settings.output_dir or settings.data_dir, else raise.

    Call it before every write (mkdir, file create, db create). The roots are refused outright if either is
    the replay directory or one of its parents, so a misconfigured output_dir cannot open up the originals.
    Tests get their "pytest tmp dir" by pointing output_dir/data_dir at it.
    """
    target = Path(path).resolve(strict=False)
    replay = settings.replay_dir.resolve(strict=False)
    roots = [settings.output_dir.resolve(strict=False), settings.data_dir.resolve(strict=False)]
    for root in roots:
        if root == replay or replay.is_relative_to(root):
            raise WriteRefused(f"{root} is (or contains) the replay directory {replay}; refusing all writes")
    if not any(target.is_relative_to(root) for root in roots):
        raise WriteRefused(f"{target} is outside output_dir ({roots[0]}) and data_dir ({roots[1]})")
    return target
