"""Retention plan (P4.2): which of our own files would go first to get under a rolling disk cap.

    plan(settings, store, now=None) -> Plan        (`autoclipper retention [--json]`)

REPORT-ONLY. This module has no code path that deletes, moves or rewrites anything: it lists files and sums
sizes. Whether to act on the plan is a decision for Jay; enabling deletion means adding a separate, explicit
step (with its own tests and a dry-run), not flipping a flag here. A test greps this file to keep it so.

What counts (the "managed" files, summed against AUTOCLIPPER_RETENTION_GB):

    output   clip files autoclipper rendered under output_dir (`clipNN_NNNNs_{cut,share}.mp4`, `_thumb.jpg`,
             `_preview.webp`)
    upload   video files in the upload inbox (`<inbox>/<user>/<sha256>/`; `.staging/` is never listed)
    recorder video files under AUTOCLIPPER_RECORDER_DIR (unset = none) that carry Segra's embedded metadata
             (`hints.segra_metadata`), i.e. were made by the Segra fork

Nothing else is ever listed: not OBS `Replay *` files, not other files in replay_dir or the recorder dir.
A managed file is a candidate only if none of these protects it:

    kept        it is a file of a clip whose candidate the judge kept (clips are the product)
    recent      modified less than AUTOCLIPPER_RETENTION_MIN_AGE_DAYS ago
    unprocessed (upload/recorder) the store has no finished (done/dead/duplicate) row for it yet, so
                it has not been mined for clips

Candidates are ordered oldest first (mtime) and taken until the managed total would fit the cap. If the
candidates run out first the plan says it falls short.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import hints
from .config import Settings
from .store import TERMINAL, Store
from .uploads import inbox_root

VIDEO_EXTS = (".mp4", ".mkv", ".mov")
CLIP_FILE = re.compile(r"^clip\d{2}_\d{4}s_(cut\.mp4|share\.mp4|thumb\.jpg|preview\.webp)$")
DAY = 86400.0
GB = 1024**3


@dataclass(frozen=True)
class Entry:
    path: Path
    size: int
    mtime: float
    source: str  # output | upload | recorder
    age_days: float
    eligible: bool
    reason: str  # why it is a candidate, or why it is protected

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path), "size": self.size, "mtime": self.mtime, "source": self.source,
            "age_days": round(self.age_days, 2), "eligible": self.eligible, "reason": self.reason,
        }  # fmt: skip


@dataclass
class Plan:
    cap_bytes: int
    min_age_days: float
    managed: list[Entry]  # everything we manage, protected files included
    delete_order: list[Entry]  # the oldest-first files that would go to reach the cap (report only)

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.managed)

    @property
    def plan_bytes(self) -> int:
        return sum(e.size for e in self.delete_order)

    @property
    def after_bytes(self) -> int:
        return self.total_bytes - self.plan_bytes

    @property
    def over_bytes(self) -> int:
        return max(0, self.total_bytes - self.cap_bytes)

    def to_dict(self) -> dict[str, Any]:
        protected = [e for e in self.managed if not e.eligible]
        return {
            "report_only": True,
            "cap_bytes": self.cap_bytes,
            "min_age_days": self.min_age_days,
            "total_bytes": self.total_bytes,
            "over_bytes": self.over_bytes,
            "managed_files": len(self.managed),
            "eligible_files": sum(1 for e in self.managed if e.eligible),
            "protected_files": len(protected),
            "protected_bytes": sum(e.size for e in protected),
            "plan_bytes": self.plan_bytes,
            "after_bytes": self.after_bytes,
            "reaches_cap": self.after_bytes <= self.cap_bytes,
            "plan": [e.to_dict() for e in self.delete_order],
        }


def _videos(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.suffix.lower() in VIDEO_EXTS and p.is_file())


def _kept_paths(store: Store) -> set[Path]:
    rows = store.con.execute(
        "SELECT cl.cut_path, cl.share_path, cl.thumb_path, cl.webp_path FROM clips cl "
        "JOIN candidates c ON c.id = cl.candidate_id WHERE c.keep = 1"
    ).fetchall()
    return {Path(p).resolve(strict=False) for row in rows for p in tuple(row) if p}


def _finished_paths(store: Store) -> set[Path]:
    marks = ",".join("?" * len(TERMINAL))
    rows = store.con.execute(f"SELECT path FROM replays WHERE status IN ({marks})", TERMINAL).fetchall()
    return {Path(r["path"]).resolve(strict=False) for r in rows}


def _entry(path: Path, source: str, now: float, min_age_days: float, protect: str, why: str) -> Entry | None:
    try:
        st = path.stat()
    except OSError:
        return None
    age = max(0.0, (now - st.st_mtime) / DAY)
    if not protect and age < min_age_days:
        protect = f"recent: {age:.1f} days old, under the {min_age_days:g}-day minimum"
    reason = protect or f"{why}, {age:.1f} days old"
    return Entry(path, st.st_size, st.st_mtime, source, age, not protect, reason)


def managed_files(settings: Settings, store: Store, now: float, min_age_days: float) -> list[Entry]:
    kept, finished = _kept_paths(store), _finished_paths(store)
    seen: set[Path] = set()
    out: list[Entry] = []

    def add(path: Path, source: str, protect: str, why: str) -> None:
        real = path.resolve(strict=False)
        if real in seen:
            return
        seen.add(real)
        if (e := _entry(path, source, now, min_age_days, protect, why)) is not None:
            out.append(e)

    out_root = settings.output_dir
    if out_root.is_dir():
        for p in sorted(out_root.rglob("*")):
            if p.is_file() and CLIP_FILE.match(p.name):
                protect = "kept: file of a kept clip" if p.resolve(strict=False) in kept else ""
                add(p, "output", protect, "autoclipper output, not a kept clip")

    def processed(p: Path, label: str) -> tuple[str, str]:
        if p.resolve(strict=False) in finished:
            return "", f"{label}, already processed"
        return f"unprocessed: {label} has no finished row in the store yet", ""

    inbox = inbox_root(settings)
    for p in _videos(inbox):
        if ".staging" in p.relative_to(inbox).parts:
            continue
        protect, why = processed(p, "upload")
        add(p, "upload", protect, why)

    rec = settings.recorder_dir
    if rec is not None:
        for p in _videos(rec):
            if p.name.startswith("Replay ") or hints.segra_metadata(p) is None:
                continue  # not made by the Segra fork (or an OBS replay): never ours to touch
            protect, why = processed(p, "Segra recording (SEGRA-METADATA)")
            add(p, "recorder", protect, why)
    return out


def plan(settings: Settings, store: Store, now: float | None = None) -> Plan:
    now = time.time() if now is None else now
    cap = int(settings.retention_gb * GB)
    managed = managed_files(settings, store, now, settings.retention_min_age_days)
    total = sum(e.size for e in managed)
    order: list[Entry] = []
    for e in sorted((e for e in managed if e.eligible), key=lambda e: (e.mtime, str(e.path))):
        if total <= cap:
            break
        order.append(e)
        total -= e.size
    return Plan(cap, settings.retention_min_age_days, managed, order)


def _gb(n: int) -> str:
    return f"{n / GB:.2f} GB"


def format_plan(p: Plan) -> str:
    d = p.to_dict()
    lines = [
        "retention plan (REPORT ONLY: nothing is deleted)",
        f"managed: {d['managed_files']} file(s), {_gb(p.total_bytes)}   cap: {_gb(p.cap_bytes)}   "
        f"over: {_gb(p.over_bytes)}   min age: {p.min_age_days:g} days",
        f"candidates: {d['eligible_files']}   protected: {d['protected_files']} "
        f"({_gb(d['protected_bytes'])})",
    ]
    if not p.delete_order:
        lines.append("plan: nothing to do" if not p.over_bytes else "plan: over the cap but no candidates")
    else:
        lines.append(f"plan: {len(p.delete_order)} file(s), {_gb(p.plan_bytes)}, oldest first")
        lines += [f"  {_gb(e.size):>10}  {e.path}\n{'':14}[{e.source}] {e.reason}" for e in p.delete_order]
        lines.append(
            f"after: {_gb(p.after_bytes)}"
            + ("" if d["reaches_cap"] else "  (still over the cap: not enough candidates)")
        )
    return "\n".join(lines)
