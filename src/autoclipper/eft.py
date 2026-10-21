"""Escape from Tarkov raids from the client's application logs (read-only; no memory access, no injection).

Mirrors the recorder's `Backend/Games/Tarkov/TarkovLogParser.cs` so hub and recorder agree on what a raid is:

    map         `TRACE-NetworkGameCreate profileStatus: '..., Location: <id>, ...'` (ignored while in a raid)
    raid start  `GameStarted:<n> real:<n> diff:<n>`
    raid end    the first `PrepareSelectedProfileLocally` after a raid start (the menu reloads the profile)
    `Network game matching cancelled.` outside a raid forgets the pending map.

Lines are `yyyy-MM-dd HH:mm:ss.fff|version|Level|application|message` in the PC's local time; datetimes
here are naive local time too, like recording file names. Each `log_*` directory (one game launch) starts a
fresh parser. A raid still open when its log directory ends is *interrupted*: it ends at that log's last
line if a newer log directory exists (the game was restarted), otherwise it is open-ended (`end=None`,
clipped to the recording's end, as the recorder does when it stops).

    read_raids(logs_dir, since, until) -> list[Raid]
    segments(raids, rec_start, duration) -> list[Segment]   raids in recording seconds
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

MAP_NAMES = {
    "bigmap": "Customs",
    "factory4_day": "Factory",
    "factory4_night": "Night Factory",
    "woods": "Woods",
    "shoreline": "Shoreline",
    "interchange": "Interchange",
    "rezervbase": "Reserve",
    "laboratory": "The Lab",
    "lighthouse": "Lighthouse",
    "tarkovstreets": "Streets of Tarkov",
    "sandbox": "Ground Zero",
    "sandbox_high": "Ground Zero",
    "labyrinth": "Labyrinth",
}

_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\|[^|]*\|[^|]*\|application\|(.*)$")
_LOCATION = re.compile(r"^TRACE-NetworkGameCreate profileStatus:.*[ ,']Location: ([A-Za-z0-9_]+)")
_DIR_STAMP = re.compile(r"^log_(\d{4})\.(\d{2})\.(\d{2})_(\d{2})-(\d{2})-(\d{2})")
_FILE_GLOB = "* application_???.log"


def map_name(map_id: str | None) -> str | None:
    return None if map_id is None else MAP_NAMES.get(map_id.lower(), map_id)


@dataclass(frozen=True)
class Event:
    kind: str  # "map" | "raid_start" | "raid_end"
    time: datetime
    map_id: str | None
    interrupted: bool = False


class LogParser:
    """Pure line parser (no I/O); feed lines in order. Same rules as TarkovLogParser.cs."""

    def __init__(self) -> None:
        self.map_id: str | None = None
        self.in_raid = False
        self.last_time: datetime | None = None

    def parse(self, line: str) -> Event | None:
        line = line.lstrip("﻿").rstrip("\r\n")
        if len(line) < 30 or "|application|" not in line:
            return None
        m = _LINE.match(line)
        if not m:
            return None
        try:
            t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            return None
        self.last_time = t
        msg = m.group(2)
        if msg.startswith("TRACE-NetworkGameCreate profileStatus:"):
            loc = _LOCATION.match(msg)
            if not loc or self.in_raid:
                return None
            self.map_id = loc.group(1)
            return Event("map", t, self.map_id)
        if msg.startswith("GameStarted:"):
            if self.in_raid:
                return None
            self.in_raid = True
            return Event("raid_start", t, self.map_id)
        if msg.startswith("PrepareSelectedProfileLocally"):
            if not self.in_raid:
                return None
            ev = Event("raid_end", t, self.map_id)
            self.in_raid, self.map_id = False, None
            return ev
        if not self.in_raid and msg.startswith("Network game matching cancelled"):
            self.map_id = None
        return None

    def end_of_stream(self, t: datetime | None) -> Event | None:
        """The log ends here. An open raid becomes an interrupted raid_end at `t` (None: open-ended)."""
        if not self.in_raid:
            return None
        ev = Event("raid_end", t or datetime.max, self.map_id, interrupted=True)
        self.in_raid, self.map_id = False, None
        return ev


@dataclass(frozen=True)
class Raid:
    map_id: str | None
    map_name: str | None
    start: datetime  # GameStarted
    end: datetime | None  # None: still open when the logs end
    interrupted: bool = False


@dataclass(frozen=True)
class Segment:
    """A raid as seen by one recording: [start, end) in recording seconds, clipped to the recording."""

    start: float
    end: float
    map_id: str | None
    map_name: str | None
    raid_start: str  # ISO local wall clock of GameStarted
    raid_end: str  # ISO local wall clock of the raid end ("" if open-ended)
    interrupted: bool = False
    kind: str = "raid"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"], d["end"] = round(self.start, 3), round(self.end, 3)
        return d

    @property
    def label(self) -> str:
        return self.map_name or "unknown map"


def dir_start(d: Path) -> datetime | None:
    m = _DIR_STAMP.match(d.name)
    return datetime(*map(int, m.groups())) if m else None  # type: ignore[arg-type]


def log_dirs(logs_dir: Path) -> list[Path]:
    """`log_*` directories, oldest first (the names sort chronologically)."""
    if not logs_dir.is_dir():
        return []
    return sorted((d for d in logs_dir.glob("log_*") if d.is_dir()), key=lambda d: d.name)


def _lines(files: Iterable[Path]) -> Iterator[str]:
    for f in files:
        with f.open(encoding="utf-8", errors="replace") as fh:
            yield from fh


def raids_in_dir(d: Path, newer_exists: bool) -> list[Raid]:
    parser = LogParser()
    raids: list[Raid] = []
    start: Event | None = None
    events = [parser.parse(line) for line in _lines(sorted(d.glob(_FILE_GLOB), key=lambda f: f.name))]
    events.append(parser.end_of_stream(parser.last_time if newer_exists else None))
    for ev in events:
        if ev is None or ev.kind == "map":
            continue
        if ev.kind == "raid_start":
            start = ev
        elif ev.kind == "raid_end" and start is not None:
            end = None if ev.time == datetime.max else ev.time
            raids.append(Raid(start.map_id, map_name(start.map_id), start.time, end, ev.interrupted))
            start = None
    return raids


def read_raids(
    logs_dir: Path | str, since: datetime | None = None, until: datetime | None = None
) -> list[Raid]:
    """All raids in `logs_dir`, or only those that can overlap [since, until] (log dirs pruned by name)."""
    dirs = log_dirs(Path(logs_dir))
    picked: list[tuple[int, Path]] = []
    for i, d in enumerate(dirs):
        st = dir_start(d)
        nxt = dir_start(dirs[i + 1]) if i + 1 < len(dirs) else None
        if until is not None and st is not None and st > until:
            continue
        if since is not None and nxt is not None and nxt <= since:
            continue  # the next launch began before the window, so this one ended before it too
        picked.append((i, d))
    raids: list[Raid] = []
    for i, d in picked:
        raids += raids_in_dir(d, newer_exists=i + 1 < len(dirs))
    return raids


def segments(raids: Iterable[Raid], rec_start: datetime, duration: float) -> list[Segment]:
    """Raids overlapping [rec_start, rec_start + duration], in recording seconds."""
    rec_end = rec_start + timedelta(seconds=duration)
    out = []
    for r in raids:
        end = r.end or rec_end
        if end <= rec_start or r.start >= rec_end:
            continue
        a = max(0.0, (r.start - rec_start).total_seconds())
        b = min(duration, (end - rec_start).total_seconds())
        out.append(Segment(
            a, b, r.map_id, r.map_name, r.start.isoformat(sep=" "),
            r.end.isoformat(sep=" ") if r.end else "", r.interrupted or r.end is None,
        ))  # fmt: skip
    return sorted(out, key=lambda s: s.start)


def segment_at(segs: Iterable[Segment], t: float) -> Segment | None:
    return next((s for s in segs if s.start <= t < s.end), None)
