"""Full-text search over judged candidates (SQLite FTS5, table `search`, store schema v4).

One row per judged candidate: the judge's title, summary, type and game, the map label of the raid segment
the window falls in (long recordings) and the transcript words spoken inside the window (from the replay's
signals timeline). Rows are written by `Store.set_judgement` as verdicts arrive and rebuilt from scratch by
the v4 migration (and `Store.rebuild_search`), so the index is always derivable from the other tables.

    search(con, "firefight", limit=10) -> [{candidate_id, replay_id, start, end, title, score, snippet, ...}]

User input is never passed to MATCH as written: it is reduced to quoted word tokens (implicit AND, falling
back to OR when nothing matches all of them), so FTS syntax characters cannot raise errors. `score` is
-bm25 (higher is better; title counts most; a word in nearly every row scores ~0 and ties fall to
keep, then signal score). Snippets mark hits with MARK_OPEN/MARK_CLOSE control characters
so callers can escape the text first and then highlight.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

MARK_OPEN, MARK_CLOSE = "\x02", "\x03"
SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    title, summary, type, game, map, transcript, candidate_id UNINDEXED,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
"""
_WEIGHTS = "8.0, 4.0, 2.0, 2.0, 2.0, 1.0, 0.0"  # per column, in table order
MAX_TOKENS = 12


def window_transcript(segments: list[dict], start: float, end: float) -> str:
    """Text of the transcript segments overlapping [start, end], in time order."""
    hits = [s for s in segments if s.get("end", 0) > start and s.get("start", 0) < end]
    hits.sort(key=lambda s: s.get("start", 0))
    return " ".join(str(s.get("text", "")).strip() for s in hits).strip()


def load_transcript(timeline_path: str) -> list[dict]:
    """The `transcript` list of a signals timeline; [] when missing or unreadable."""
    if not timeline_path:
        return []
    try:
        data = json.loads(Path(timeline_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    tr = data.get("transcript") if isinstance(data, dict) else None
    return [s for s in tr if isinstance(s, dict)] if isinstance(tr, list) else []


def _map_label(con: sqlite3.Connection, replay_id: int, start: float, end: float) -> str:
    rows = con.execute(
        'SELECT map_id, label FROM segments WHERE replay_id = ? AND "end" > ? AND start < ? ORDER BY idx',
        (replay_id, start, end),
    )
    return " ".join(dict.fromkeys(w for r in rows for w in (r["map_id"], r["label"]) if w))


def _index_row(con: sqlite3.Connection, cand: sqlite3.Row, transcript: list[dict]) -> None:
    con.execute("DELETE FROM search WHERE candidate_id = ?", (cand["id"],))
    try:
        v = json.loads(cand["verdict"] or "")
    except ValueError:
        v = None
    if cand["judge_status"] != "judged" or not isinstance(v, dict):
        return
    con.execute(
        "INSERT INTO search(title, summary, type, game, map, transcript, candidate_id) VALUES(?,?,?,?,?,?,?)",
        (str(v.get("title") or ""), str(v.get("summary") or ""), str(v.get("type") or ""),
         str(v.get("game") or ""), _map_label(con, cand["replay_id"], cand["start"], cand["end"]),
         window_transcript(transcript, cand["start"], cand["end"]), cand["id"]),
    )  # fmt: skip


def index_candidate(con: sqlite3.Connection, cid: int) -> None:
    """(Re)index one candidate, or drop it from the index when it is not judged. Caller commits."""
    cand = con.execute(
        "SELECT c.*, r.timeline_path FROM candidates c JOIN replays r ON r.id = c.replay_id WHERE c.id = ?",
        (cid,),
    ).fetchone()
    if cand is None:
        con.execute("DELETE FROM search WHERE candidate_id = ?", (cid,))
        return
    judged = cand["judge_status"] == "judged"
    _index_row(con, cand, load_transcript(cand["timeline_path"]) if judged else [])


def rebuild(con: sqlite3.Connection) -> int:
    """Drop and refill the whole index from candidates, segments and timelines. Caller commits."""
    con.execute("DELETE FROM search")
    n = 0
    for r in con.execute("SELECT id, timeline_path FROM replays").fetchall():
        rows = con.execute(
            "SELECT * FROM candidates WHERE replay_id = ? AND judge_status = 'judged'", (r["id"],)
        ).fetchall()
        if not rows:
            continue
        transcript = load_transcript(r["timeline_path"])  # once per replay, not per candidate
        for cand in rows:
            _index_row(con, cand, transcript)
            n += 1
    return n


def match_queries(text: str) -> list[str]:
    """MATCH expressions for free text: every word quoted (AND first, then OR). [] when there are no words."""
    words = list(dict.fromkeys(w.lower() for w in re.findall(r"\w+", text[:500])))[:MAX_TOKENS]
    if not words:
        return []
    quoted = [f'"{w}"' for w in words]
    return [" ".join(quoted)] + ([" OR ".join(quoted)] if len(quoted) > 1 else [])


def search(con: sqlite3.Connection, text: str, limit: int = 10) -> list[dict[str, Any]]:
    """Best matches first. Each dict: candidate_id, replay_id, replay_path, start, end, title, score
    (-bm25), snippet (hits wrapped in MARK_OPEN/MARK_CLOSE), keep, clip_path (share mp4, '' if none)."""
    sql = (
        f"SELECT c.id AS candidate_id, c.replay_id, r.path AS replay_path, c.start, c.\"end\" AS \"end\", "
        f"c.keep, s.title, -bm25(search, {_WEIGHTS}) AS score, "
        f"snippet(search, -1, '{MARK_OPEN}', '{MARK_CLOSE}', '...', 14) AS snippet, "
        "COALESCE(k.share_path, '') AS clip_path, COALESCE(k.status, '') AS clip_status "
        "FROM search s JOIN candidates c ON c.id = s.candidate_id JOIN replays r ON r.id = c.replay_id "
        "LEFT JOIN clips k ON k.candidate_id = c.id WHERE search MATCH ? ORDER BY ROUND(bm25(search, "
        f"{_WEIGHTS}), 4), c.keep DESC, c.score DESC, c.id LIMIT ?"
    )  # fmt: skip
    for expr in match_queries(text):
        try:
            rows = con.execute(sql, (expr, max(1, limit))).fetchall()
        except sqlite3.OperationalError:
            return []
        if rows:
            out = [dict(r) for r in rows]
            for d in out:
                if d.pop("clip_status") != "ok":
                    d["clip_path"] = ""
                d["score"] = round(d["score"], 3)
            return out
    return []


def highlight(snippet: str, escape: Any) -> str:
    """HTML for a snippet: escaped by `escape`, hits in <mark>."""
    return escape(snippet).replace(MARK_OPEN, "<mark>").replace(MARK_CLOSE, "</mark>")
