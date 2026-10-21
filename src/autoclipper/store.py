"""SQLite store (`data_dir/autoclipper.sqlite`): replays, per-stage timings, candidates, clips, labels.

The schema is versioned with `PRAGMA user_version` and created idempotently (`MIGRATIONS` run in order, each
once). Replay identity is (sha256, path); a second path with the same hash is recorded as a duplicate.

    replays     one row per file seen; status new|queued|done|dead|failed|duplicate
    stages      per replay and stage (probe, dead_check, analyze, candidates, judge, render): status, runs,
                accumulated seconds (judge re-runs add up)
    candidates  window + signal score; judge_status pending|judged|queued|failed, verdict JSON, model, cost
    clips       output paths (cut, share mp4, thumb, webp) and the posted flag
    labels      thumbs 'up'/'down' per clip, for the review page
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .config import Settings
from .safety import assert_writable

DB_NAME = "autoclipper.sqlite"
TERMINAL = ("done", "dead", "duplicate")

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS replays (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    duration REAL,
    status TEXT NOT NULL DEFAULT 'new',
    dead INTEGER NOT NULL DEFAULT 0,
    dead_reason TEXT NOT NULL DEFAULT '',
    duplicate_of INTEGER REFERENCES replays(id),
    timeline_path TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (sha256, path)
);
CREATE INDEX IF NOT EXISTS replays_path ON replays(path);
CREATE TABLE IF NOT EXISTS stages (
    replay_id INTEGER NOT NULL REFERENCES replays(id),
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    runs INTEGER NOT NULL DEFAULT 1,
    seconds REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    finished_at REAL NOT NULL,
    PRIMARY KEY (replay_id, stage)
);
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    replay_id INTEGER NOT NULL REFERENCES replays(id),
    idx INTEGER NOT NULL,
    start REAL NOT NULL,
    "end" REAL NOT NULL,
    score REAL NOT NULL,
    data TEXT NOT NULL,
    judge_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (judge_status IN ('pending','judged','queued','failed')),
    keep INTEGER,
    verdict TEXT,
    model TEXT NOT NULL DEFAULT '',
    cost_usd REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    judged_at REAL,
    UNIQUE (replay_id, start, "end")
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL UNIQUE REFERENCES candidates(id),
    status TEXT NOT NULL,
    out_dir TEXT NOT NULL DEFAULT '',
    cut_path TEXT NOT NULL DEFAULT '',
    share_path TEXT NOT NULL DEFAULT '',
    thumb_path TEXT NOT NULL DEFAULT '',
    webp_path TEXT NOT NULL DEFAULT '',
    share_bytes INTEGER NOT NULL DEFAULT 0,
    encoder TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    posted INTEGER NOT NULL DEFAULT 0,
    posted_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL REFERENCES clips(id),
    label TEXT NOT NULL CHECK (label IN ('up','down')),
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""

MIGRATIONS = [SCHEMA_V1]  # index + 1 is the user_version it brings the db to
SCHEMA_VERSION = len(MIGRATIONS)

_REPLAY_COLS = frozenset({
    "duration", "status", "dead", "dead_reason", "duplicate_of", "timeline_path", "error", "mtime", "size",
})  # fmt: skip
_CLIP_COLS = frozenset({
    "status", "out_dir", "cut_path", "share_path", "thumb_path", "webp_path", "share_bytes", "encoder", "error",  # noqa: E501
})  # fmt: skip


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


class Store:
    def __init__(self, settings: Settings, path: Path | None = None):
        self.path = Path(path) if path else settings.data_dir / DB_NAME
        assert_writable(self.path.parent, settings)
        assert_writable(self.path, settings)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path, timeout=30)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        version = self.con.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"{self.path} has schema v{version}, newer than this code (v{SCHEMA_VERSION})")
        for i in range(version, SCHEMA_VERSION):
            with self.con:
                self.con.executescript(MIGRATIONS[i])
                self.con.execute(f"PRAGMA user_version = {i + 1}")

    @property
    def version(self) -> int:
        return self.con.execute("PRAGMA user_version").fetchone()[0]

    # replays

    def replay(self, rid: int) -> dict[str, Any]:
        return dict(self.con.execute("SELECT * FROM replays WHERE id = ?", (rid,)).fetchone())

    def replay_by_stat(self, path: Path | str, size: int, mtime: float) -> dict[str, Any] | None:
        """The row for this path with the same size and mtime (so its hash can be reused without reading)."""
        return _row(self.con.execute(
            "SELECT * FROM replays WHERE path = ? AND size = ? AND mtime = ? ORDER BY status = 'duplicate', id LIMIT 1",  # noqa: E501
            (str(path), size, mtime),
        ).fetchone())  # fmt: skip

    def original_of(self, sha256: str, path: Path | str) -> dict[str, Any] | None:
        """Another non-duplicate row with this hash at a different path, if any (oldest first)."""
        return _row(self.con.execute(
            "SELECT * FROM replays WHERE sha256 = ? AND path != ? AND status != 'duplicate' ORDER BY id LIMIT 1",  # noqa: E501
            (sha256, str(path)),
        ).fetchone())  # fmt: skip

    def upsert_replay(self, path: Path | str, sha256: str, size: int, mtime: float) -> dict[str, Any]:
        now = time.time()
        with self.con:
            self.con.execute(
                "INSERT INTO replays(path, sha256, size, mtime, created_at, updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(sha256, path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, "
                "updated_at=excluded.updated_at",
                (str(path), sha256, size, mtime, now, now),
            )
        return dict(self.con.execute(
            "SELECT * FROM replays WHERE sha256 = ? AND path = ?", (sha256, str(path))
        ).fetchone())  # fmt: skip

    def update_replay(self, rid: int, **fields: Any) -> None:
        bad = set(fields) - _REPLAY_COLS
        if bad:
            raise ValueError(f"unknown replay columns {sorted(bad)}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.con:
            self.con.execute(
                f"UPDATE replays SET {cols}, updated_at = ? WHERE id = ?",
                (*fields.values(), time.time(), rid),
            )

    # stages

    def stage_done(self, rid: int, stage: str) -> bool:
        r = self.con.execute(
            "SELECT status FROM stages WHERE replay_id = ? AND stage = ?", (rid, stage)
        ).fetchone()
        return r is not None and r["status"] == "ok"

    def record_stage(self, rid: int, stage: str, seconds: float, status: str = "ok", error: str = "") -> None:
        """Upsert a stage result; seconds and runs accumulate over re-runs (judge passes add up)."""
        with self.con:
            self.con.execute(
                "INSERT INTO stages(replay_id, stage, status, runs, seconds, error, finished_at) "
                "VALUES(?,?,?,1,?,?,?) ON CONFLICT(replay_id, stage) DO UPDATE SET status=excluded.status, "
                "runs=runs+1, seconds=seconds+excluded.seconds, error=excluded.error, "
                "finished_at=excluded.finished_at",
                (rid, stage, status, seconds, error[:500], time.time()),
            )

    def stages(self, rid: int) -> dict[str, dict[str, Any]]:
        rows = self.con.execute("SELECT * FROM stages WHERE replay_id = ?", (rid,)).fetchall()
        return {r["stage"]: dict(r) for r in rows}

    # candidates

    def add_candidates(self, rid: int, candidates: Iterable[dict]) -> None:
        with self.con:
            for i, c in enumerate(candidates):
                self.con.execute(
                    'INSERT OR IGNORE INTO candidates(replay_id, idx, start, "end", score, data) VALUES(?,?,?,?,?,?)',  # noqa: E501
                    (rid, i, c["start"], c["end"], c["score"], json.dumps(c)),
                )

    def candidates(self, rid: int, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        rows = [dict(r) for r in self.con.execute(
            "SELECT * FROM candidates WHERE replay_id = ? ORDER BY idx, id", (rid,)
        )]  # fmt: skip
        if statuses is not None:
            wanted = set(statuses)
            rows = [r for r in rows if r["judge_status"] in wanted]
        for r in rows:
            r["data"] = json.loads(r["data"])
            r["verdict"] = json.loads(r["verdict"]) if r["verdict"] else None
        return rows

    def reset_failed_candidates(self, rid: int) -> None:
        with self.con:
            self.con.execute(
                "UPDATE candidates SET judge_status='pending', error='' WHERE replay_id = ? AND judge_status='failed'",  # noqa: E501
                (rid,),
            )

    def set_judgement(
        self, cid: int, status: str, *, verdict: dict | None = None, model: str = "", cost_usd: float = 0.0,
        error: str = "", called: bool = True,
    ) -> None:  # fmt: skip
        keep = None if verdict is None or status != "judged" else int(bool(verdict.get("keep")))
        with self.con:
            self.con.execute(
                "UPDATE candidates SET judge_status=?, keep=?, verdict=?, model=?, cost_usd=cost_usd+?, "
                "attempts=attempts+?, error=?, judged_at=? WHERE id=?",
                (status, keep, json.dumps(verdict) if verdict else None, model, cost_usd, int(called),
                 error[:500], time.time(), cid),
            )  # fmt: skip

    # clips

    def clip_for(self, cid: int) -> dict[str, Any] | None:
        return _row(self.con.execute("SELECT * FROM clips WHERE candidate_id = ?", (cid,)).fetchone())

    def save_clip(self, cid: int, **fields: Any) -> int:
        bad = set(fields) - _CLIP_COLS
        if bad:
            raise ValueError(f"unknown clip columns {sorted(bad)}")
        with self.con:
            self.con.execute(
                "INSERT OR IGNORE INTO clips(candidate_id, status, created_at) VALUES(?,?,?)",
                (cid, fields.get("status", "ok"), time.time()),
            )
            cols = ", ".join(f"{k} = ?" for k in fields)
            self.con.execute(f"UPDATE clips SET {cols} WHERE candidate_id = ?", (*fields.values(), cid))
        return self.con.execute("SELECT id FROM clips WHERE candidate_id = ?", (cid,)).fetchone()["id"]

    def add_label(self, clip_id: int, label: str, note: str = "") -> int:
        with self.con:
            cur = self.con.execute(
                "INSERT INTO labels(clip_id, label, note, created_at) VALUES(?,?,?,?)",
                (clip_id, label, note, time.time()),
            )
        return int(cur.lastrowid or 0)
