"""The verdict schema (shared by Claude's --json-schema and the ollama fallback) and the Verdict type."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

TYPES = ("firefight", "kill", "death", "funny", "clutch", "loot", "other")
GAMES = ("tarkov", "wardogs", "other")
SOURCES = ("claude-haiku", "claude-sonnet", "ollama", "none")
TITLE_MAX = 60
SUMMARY_MAX = 140
FIELDS = ("keep", "score", "type", "title", "summary", "start", "end", "thumb_t", "game")


class SchemaError(ValueError):
    """A model answer that doesn't fit the verdict schema."""


@dataclass(frozen=True)
class Verdict:
    keep: bool = False
    score: int = 0
    type: str = "other"
    title: str = ""
    summary: str = ""
    start: float = 0.0
    end: float = 0.0
    thumb_t: float = 0.0
    game: str = "other"
    source: str = "none"  # claude-haiku / claude-sonnet / ollama / none
    model: str = ""
    queued: bool = False  # Claude was unavailable and the caller asked to queue: judge again later
    error: str = ""
    candidate_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def verdict_schema(start: float, end: float) -> dict:
    """JSON Schema for one verdict; start/end/thumb_t are bounded to the candidate window."""
    t = {"type": "number", "minimum": round(start, 2), "maximum": round(end, 2)}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(FIELDS),
        "properties": {
            "keep": {"type": "boolean"},
            "score": {"type": "integer", "minimum": 0, "maximum": 10},
            "type": {"type": "string", "enum": list(TYPES)},
            "title": {"type": "string", "maxLength": TITLE_MAX},
            "summary": {"type": "string", "maxLength": SUMMARY_MAX},
            "start": {**t, "description": "clip start, replay seconds"},
            "end": {**t, "description": "clip end, replay seconds"},
            "thumb_t": {**t, "description": "best thumbnail frame, replay seconds"},
            "game": {"type": "string", "enum": list(GAMES)},
        },
    }


def _num(d: dict, key: str) -> float:
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        raise SchemaError(f"{key} must be a number, got {v!r}")
    return float(v)


def validate(d: object, start: float, end: float) -> dict:
    """Check an answer against the schema and normalise it.

    Missing fields, wrong types and unknown enum values raise SchemaError. Over-long strings are cut and
    times are clamped into the window (end > start), since a slightly-off answer is still useful.
    """
    if not isinstance(d, dict):
        raise SchemaError(f"verdict must be an object, got {type(d).__name__}")
    missing = [k for k in FIELDS if k not in d]
    if missing:
        raise SchemaError(f"verdict is missing {', '.join(missing)}")
    if not isinstance(d["keep"], bool):
        raise SchemaError(f"keep must be a boolean, got {d['keep']!r}")
    for k in ("type", "title", "summary", "game"):
        if not isinstance(d[k], str):
            raise SchemaError(f"{k} must be a string, got {d[k]!r}")
    if d["type"] not in TYPES:
        raise SchemaError(f"type {d['type']!r} not in {TYPES}")
    if d["game"] not in GAMES:
        raise SchemaError(f"game {d['game']!r} not in {GAMES}")
    score = _num(d, "score")
    if not 0 <= score <= 10:
        raise SchemaError(f"score {score} outside 0-10")

    def clamp(x: float) -> float:
        return round(min(max(x, start), end), 2)

    s, e, th = clamp(_num(d, "start")), clamp(_num(d, "end")), clamp(_num(d, "thumb_t"))
    if e <= s:
        s, e = round(start, 2), round(end, 2)
    return {
        "keep": d["keep"],
        "score": int(round(score)),
        "type": d["type"],
        "title": d["title"].strip()[:TITLE_MAX],
        "summary": d["summary"].strip()[:SUMMARY_MAX],
        "start": s,
        "end": e,
        "thumb_t": min(max(th, s), e),
        "game": d["game"],
    }


def is_borderline(v: Verdict) -> bool:
    """Scores 4-6 get a second opinion from the polish model."""
    return 4 <= v.score <= 6
