"""Usage guard and ledger for Claude calls. Fails closed: when in doubt, no call.

- One call at a time across processes (`fcntl.flock` on `judge.lock` next to the ledger).
- A ledger row is reserved (status 'started') before each call and updated afterwards, so crashed,
  failed and timed-out calls all count toward the caps.
- Caps: per hour (judge role), per day (polish role), and per run over every row ever (Haiku / Sonnet).
- Pauses persist in SQLite: rate-limit rejection, missing rate_limit_event (except on a timeout), usage-limit
  errors, two failed calls in a row, 5-hour or 7-day utilization over the thresholds, and auth failures
  (fatal: only `clear-pause` lifts it).
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ..config import Settings
from .claude_cli import CallResult

MIN_PAUSE_S = 15 * 60
CLAUDE_ROLES = ("judge", "polish")
NEEDS_JAY_MARKER = "## Claude judge: CLI sign-in failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    model TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    duration_s REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    cost_usd REAL,
    rl_status TEXT,
    five_hour REAL,
    seven_day REAL,
    resets_at REAL,
    candidate_id TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class GuardRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def family(model: str) -> str:
    """'haiku' or 'sonnet'; anything not recognisably Haiku counts against the tighter Sonnet cap."""
    return "haiku" if "haiku" in model.lower() else "sonnet"


def _iso(t: float | None) -> str | None:
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds") if t else None


class Guard:
    def __init__(self, settings: Settings):
        self.s = settings
        self.path = Path(settings.ledger_path)
        if os.environ.get("PYTEST_CURRENT_TEST") and self.path == Settings().ledger_path:
            raise RuntimeError("tests must point ledger_path at a temporary file, not the real ledger")
        self.lock_path = self.path.parent / "judge.lock"

    # --- storage ---------------------------------------------------------------------------------
    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """A connection that commits on success, rolls back on error, and always closes."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=60)
        try:
            con.executescript(SCHEMA)
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def _get(self, con: sqlite3.Connection, key: str, default: object = None) -> object:
        row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set(self, con: sqlite3.Connection, key: str, value: object) -> None:
        con.execute(
            "INSERT INTO state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    # --- pause -----------------------------------------------------------------------------------
    def pause(self, until: float | None, reason: str, fatal: bool = False) -> None:
        """Persist a pause. Never shortens an existing one; fatal pauses only lift via clear_pause()."""
        with self._db() as con:
            cur = self._get(con, "pause", {}) or {}
            now = time.time()
            if cur.get("fatal"):
                return
            if not fatal and cur.get("until") and cur["until"] >= (until or 0):
                return
            self._set(con, "pause", {"until": until, "reason": reason, "fatal": fatal, "set_at": now})
        if fatal:
            self._note_needs_jay(reason)

    def clear_pause(self) -> None:
        with self._db() as con:
            self._set(con, "pause", {})
            self._set(con, "needs_jay_noted", False)

    def _note_needs_jay(self, reason: str) -> None:
        path = Path(self.s.needs_jay_path)
        with self._db() as con:
            if self._get(con, "needs_jay_noted", False):
                return
            self._set(con, "needs_jay_noted", True)
        if path.is_file() and NEEDS_JAY_MARKER in path.read_text(errors="replace"):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y-%m-%d %H:%M")
        with path.open("a") as f:
            f.write(
                f"\n{NEEDS_JAY_MARKER} ({day})\n"
                f"The clip judge paused Claude calls: {reason[:200]}\n"
                "Clips are judged by local ollama models (or queued) until this is fixed.\n"
                "1. In WSL run `claude`, then `/login` with the claude.ai account (no API key).\n"
                "2. `cd ~/projects/autoclipper && uv run python -m autoclipper.judge clear-pause`.\n"
            )

    # --- checks ----------------------------------------------------------------------------------
    def _refusal(self, con: sqlite3.Connection, role: str, model: str, now: float) -> str | None:
        p = self._get(con, "pause", {}) or {}
        if p.get("fatal"):
            return f"paused until cleared: {p.get('reason')}"
        if p.get("until") and p["until"] > now:
            return f"paused until {_iso(p['until'])}: {p.get('reason')}"
        fam = family(model)
        cap = self.s.run_max_haiku if fam == "haiku" else self.s.run_max_sonnet
        n = self._family_count(con, fam)
        if n >= cap:
            return f"run cap reached: {n}/{cap} {fam} calls"
        if role == "judge":
            n = con.execute(
                "SELECT COUNT(*) FROM calls WHERE role='judge' AND ts > ?", (now - 3600,)
            ).fetchone()[0]
            if n >= self.s.judge_max_calls_per_hour:
                return f"hourly cap reached: {n}/{self.s.judge_max_calls_per_hour} judge calls"
        elif role == "polish":
            n = con.execute(
                "SELECT COUNT(*) FROM calls WHERE role='polish' AND ts > ?", (now - 86400,)
            ).fetchone()[0]
            if n >= self.s.polish_max_calls_per_day:
                return f"daily cap reached: {n}/{self.s.polish_max_calls_per_day} polish calls"
        else:
            return f"unknown role {role!r}"
        return None

    def _family_count(self, con: sqlite3.Connection, fam: str) -> int:
        rows = con.execute(
            f"SELECT model, COUNT(*) FROM calls WHERE role IN {CLAUDE_ROLES} GROUP BY model"
        ).fetchall()
        return sum(n for m, n in rows if family(m) == fam)

    def check(self, role: str, model: str) -> str | None:
        """The reason a call would be refused right now, or None (advisory; slot() re-checks)."""
        with self._db() as con:
            return self._refusal(con, role, model, time.time())

    @contextmanager
    def slot(self, role: str, model: str, candidate_id: str = "") -> Iterator[int]:
        """Hold the cross-process lock, re-check, reserve a ledger row and yield its id.

        Raises GuardRefused when the call must not happen. If the body raises, the row is marked
        'error' (it still counts).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with self._db() as con:
                    now = time.time()
                    reason = self._refusal(con, role, model, now)
                    if reason:
                        raise GuardRefused(reason)
                    row = con.execute(
                        "INSERT INTO calls(ts, model, role, status, candidate_id) VALUES(?,?,?,?,?)",
                        (now, model, role, "started", candidate_id),
                    ).lastrowid
                assert row is not None
                try:
                    yield row
                except BaseException as e:
                    with self._db() as con:
                        con.execute(
                            "UPDATE calls SET status='error', error=? WHERE id=? AND status='started'",
                            (f"{type(e).__name__}: {e}"[:500], row),
                        )
                    raise
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    # --- after a call ----------------------------------------------------------------------------
    def record(self, row: int, res: CallResult) -> None:
        """Write the call's outcome to its ledger row and apply the pause rules."""
        u, rl = res.usage or {}, res.rate_limit
        with self._db() as con:
            con.execute(
                """UPDATE calls SET status=?, duration_s=?, input_tokens=?, output_tokens=?,
                   cache_read_tokens=?, cache_creation_tokens=?, cost_usd=?, rl_status=?, five_hour=?,
                   seven_day=?, resets_at=?, error=? WHERE id=?""",
                (
                    res.status, res.duration_s, u.get("input_tokens"), u.get("output_tokens"),
                    u.get("cache_read_input_tokens"), u.get("cache_creation_input_tokens"), res.cost_usd,
                    rl.status if rl else None, rl.five_hour if rl else None, rl.seven_day if rl else None,
                    rl.resets_at if rl else None, res.error[:500] or None, row,
                ),
            )  # fmt: skip
            if rl:
                self._set(con, "last_rate_limit", {**rl.__dict__, "ts": time.time()})
        self._apply_pauses(res)

    def _apply_pauses(self, res: CallResult) -> None:
        now = time.time()
        floor = now + MIN_PAUSE_S

        def until(reset: float | None) -> float:
            return reset if reset and reset > now else floor

        if res.api_key_source not in (None, "none"):
            self.pause(None, f"claude CLI used an API key ({res.api_key_source}), not the subscription", True)
        if res.binary_missing:
            self.pause(None, res.error, fatal=True)
        if res.auth_failed:
            self.pause(None, f"claude CLI auth failure: {(res.result_text or res.stderr)[-200:]}", True)
        rl = res.rate_limit
        if rl is None and res.status != "timeout":
            self.pause(floor, "no rate_limit_event in the call output")
        if not res.ok and self._last_two_failed():
            self.pause(floor, f"two failed Claude calls in a row; last: {res.error[:120]}")
        if rl and rl.status == "rejected":
            self.pause(max(rl.resets_at or 0, floor), "rate limit rejected")
        if rl and rl.five_hour is not None and rl.five_hour >= self.s.judge_max_5h:
            self.pause(
                until(rl.five_hour_resets or rl.resets_at),
                f"5-hour utilization {rl.five_hour:.2f} >= {self.s.judge_max_5h}",
            )
        if rl and rl.seven_day is not None and rl.seven_day >= self.s.judge_max_7d:
            self.pause(
                until(rl.seven_day_resets), f"7-day utilization {rl.seven_day:.2f} >= {self.s.judge_max_7d}"
            )
        if res.limit_hit:
            self.pause(max((rl.resets_at if rl else 0) or 0, floor), f"usage limit: {res.result_text[-120:]}")

    def _last_two_failed(self) -> bool:
        with self._db() as con:
            rows = con.execute(
                f"SELECT status FROM calls WHERE role IN {CLAUDE_ROLES} ORDER BY id DESC LIMIT 2"
            ).fetchall()
        return len(rows) == 2 and all(r[0] != "ok" for r in rows)

    def record_fallback(
        self, model: str, status: str, duration_s: float, error: str, candidate_id: str
    ) -> None:
        with self._db() as con:
            con.execute(
                "INSERT INTO calls(ts, model, role, status, duration_s, candidate_id, error) "
                "VALUES(?,?,?,?,?,?,?)",
                (time.time(), model, "fallback", status, duration_s, candidate_id, error[:500] or None),
            )

    # --- reporting -------------------------------------------------------------------------------
    def status(self) -> dict:
        now = time.time()
        with self._db() as con:
            p = self._get(con, "pause", {}) or {}
            last = self._get(con, "last_rate_limit")
            hour = con.execute(
                "SELECT COUNT(*) FROM calls WHERE role='judge' AND ts > ?", (now - 3600,)
            ).fetchone()[0]
            day = con.execute(
                "SELECT COUNT(*) FROM calls WHERE role='polish' AND ts > ?", (now - 86400,)
            ).fetchone()[0]
            haiku, sonnet = self._family_count(con, "haiku"), self._family_count(con, "sonnet")
            fallback = con.execute("SELECT COUNT(*) FROM calls WHERE role='fallback'").fetchone()[0]
            totals = con.execute(
                f"SELECT COALESCE(SUM(input_tokens),0)+COALESCE(SUM(cache_read_tokens),0)"
                f"+COALESCE(SUM(cache_creation_tokens),0), COALESCE(SUM(output_tokens),0), "
                f"COALESCE(SUM(cost_usd),0) FROM calls WHERE role IN {CLAUDE_ROLES}"
            ).fetchone()
        paused = bool(p.get("fatal") or (p.get("until") and p["until"] > now))
        return {
            "paused": paused,
            "fatal": bool(p.get("fatal")) if paused else False,
            "until": _iso(p.get("until")) if paused else None,
            "until_epoch": p.get("until") if paused else None,
            "reason": p.get("reason") if paused else None,
            "counts": {
                "judge_last_hour": hour,
                "judge_max_per_hour": self.s.judge_max_calls_per_hour,
                "polish_last_day": day,
                "polish_max_per_day": self.s.polish_max_calls_per_day,
                "haiku_total": haiku,
                "run_max_haiku": self.s.run_max_haiku,
                "sonnet_total": sonnet,
                "run_max_sonnet": self.s.run_max_sonnet,
                "fallback_total": fallback,
            },  # fmt: skip
            "tokens": {"input": totals[0], "output": totals[1], "notional_cost_usd": round(totals[2], 4)},
            "thresholds": {"five_hour": self.s.judge_max_5h, "seven_day": self.s.judge_max_7d},
            "last_utilization": last,
            "ledger": str(self.path),
        }
