"""Strong hints inside a recording: OBS chapter markers and Segra bookmarks (both read-only).

    chapters(path)          ffprobe -show_chapters: [(t, title)]
    segra_metadata(path)    the JSON Segra embeds in moov/udta: a `uuid` box whose payload starts with
                            "SEGRA-METADATA\\0\\x01" (recorder: Backend/Media/EmbeddedMetadataService.cs)
    hints(path)             both, as events {"t", "kind": "hint", "phrase", "role", "pre", "post"}

Segra stores `Bookmarks: [{"Id", "Type": "Kill"|"Death"|"Assist"|"Goal"|"Manual", "Subtype", "Time":
"hh:mm:ss.fffffff"}]` and also writes each bookmark as a chapter ("Kill", "Death (Headshot)", "Bookmark" for
Manual), preceded by a "Start" chapter at 0 so players do not shift the first one. OBS hybrid MP4 writes a
chapter per "Add chapter marker" hotkey press. A chapter at t ~ 0 marks the recording start, not a moment.

Event bookmarks (kill, death, ...) sit on the moment itself; manual bookmarks and chapter markers are pressed
after it, like "clip that", so they look further back (`pre`).
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path

from .media._run import FFPROBE, run

SEGRA_BOX_ID = b"SEGRA-METADATA\x00\x01"
EVENT_TYPES = {"kill", "death", "assist", "goal"}
EVENT_PRE, EVENT_POST = 8.0, 4.0  # a kill bookmark is at the kill: the fight is mostly before it
MARKER_PRE, MARKER_POST = 20.0, 3.0  # a hotkey press comes after the moment (same as a "clip that")
START_EPS = 0.5  # chapters this close to 0 mark the recording start
DEDUP_S = 1.0  # a Segra bookmark and its own chapter are the same hint
MAX_MOOV = 256 << 20
_CONTAINERS = {b"moov", b"udta"}
_TIMESPAN = re.compile(r"^(?:(\d+)\.)?(\d+):(\d+):(\d+(?:\.\d+)?)$")


def chapters(path: Path | str) -> list[tuple[float, str]]:
    proc = run([FFPROBE, "-v", "error", "-show_chapters", "-of", "json", str(path)])
    out = []
    for c in json.loads(proc.stdout or "{}").get("chapters") or []:
        try:
            t = float(c.get("start_time"))
        except (TypeError, ValueError):
            continue
        out.append((t, str((c.get("tags") or {}).get("title") or "")))
    return sorted(out)


def _boxes(buf: bytes, pos: int, end: int):
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", buf[pos : pos + 8])
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size, header = struct.unpack(">Q", buf[pos + 8 : pos + 16])[0], 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield kind, pos + header, pos + size
        pos += size


def _find_moov(path: Path) -> bytes | None:
    with path.open("rb") as f:
        f.seek(0, 2)
        total = f.tell()
        pos = 0
        while pos + 8 <= total:
            f.seek(pos)
            head = f.read(16)
            size, kind = struct.unpack(">I4s", head[:8])
            header = 8
            if size == 1:
                size, header = struct.unpack(">Q", head[8:16])[0], 16
            elif size == 0:
                size = total - pos
            if size < header:
                return None
            if kind == b"moov":
                if size > MAX_MOOV:
                    return None
                f.seek(pos + header)
                return f.read(size - header)
            pos += size
    return None


def segra_metadata(path: Path | str) -> dict | None:
    """Segra's embedded metadata (Game, Title, CreatedAt, Bookmarks, ...) or None."""
    try:
        moov = _find_moov(Path(path))
    except (OSError, struct.error):
        return None
    if not moov:
        return None
    for kind, a, b in _boxes(moov, 0, len(moov)):
        if kind != b"udta":
            continue
        for k2, a2, b2 in _boxes(moov, a, b):
            if k2 == b"uuid" and moov[a2 : a2 + len(SEGRA_BOX_ID)] == SEGRA_BOX_ID:
                try:
                    return json.loads(moov[a2 + len(SEGRA_BOX_ID) : b2].decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return None
    return None


def parse_timespan(s: str) -> float | None:
    """.NET TimeSpan "c" format ("[d.]hh:mm:ss[.fffffff]") in seconds."""
    m = _TIMESPAN.match(str(s).strip())
    if not m:
        return None
    days, h, mnt, sec = m.groups()
    return int(days or 0) * 86400 + int(h) * 3600 + int(mnt) * 60 + float(sec)


def _hint(t: float, label: str, source: str) -> dict:
    event = label.split(" ")[0].lower() in EVENT_TYPES
    pre, post = (EVENT_PRE, EVENT_POST) if event else (MARKER_PRE, MARKER_POST)
    return {"t": round(t, 3), "kind": "hint", "phrase": label, "role": source, "pre": pre, "post": post}


def hints(path: Path | str, meta: dict | None = None) -> list[dict]:
    """Hint events of a recording, sorted by time (Segra bookmarks win over their own chapters)."""
    out: list[dict] = []
    meta = meta if meta is not None else segra_metadata(path)
    for b in (meta or {}).get("Bookmarks") or []:
        t = parse_timespan(b.get("Time", ""))
        if t is None:
            continue
        label = "Bookmark" if b.get("Type") in (None, "Manual") else str(b["Type"])
        if b.get("Subtype"):
            label += f" ({b['Subtype']})"
        out.append(_hint(t, label, "segra"))
    for t, title in chapters(path):
        if t < START_EPS:
            continue
        if any(abs(h["t"] - t) <= DEDUP_S for h in out):
            continue
        out.append(_hint(t, title or "chapter", "chapter"))
    return sorted(out, key=lambda h: h["t"])
