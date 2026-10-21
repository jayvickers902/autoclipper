"""`autoclipper report`: counts, keeps, per-stage timing averages and judge calls from the store."""

from __future__ import annotations

from typing import Any

from .pipeline import STAGES
from .store import Store


def _counts(store: Store, table: str, column: str) -> dict[str, int]:
    rows = store.con.execute(f"SELECT {column} AS k, COUNT(*) AS n FROM {table} GROUP BY {column}")  # noqa: S608
    return {r["k"]: r["n"] for r in rows}


def build_report(store: Store) -> dict[str, Any]:
    q = store.con.execute
    stages = {
        r["stage"]: {"n": r["n"], "avg_s": round(r["avg"], 2), "total_s": round(r["total"], 1), "runs": r["runs"]}  # noqa: E501
        for r in q(
            "SELECT stage, COUNT(*) AS n, SUM(seconds) / COUNT(*) AS avg, SUM(seconds) AS total, SUM(runs) AS runs "  # noqa: E501
            "FROM stages WHERE status = 'ok' GROUP BY stage"
        )
    }  # fmt: skip
    judge = q(
        "SELECT COALESCE(SUM(attempts),0) AS calls, COALESCE(SUM(cost_usd),0) AS cost FROM candidates"
    ).fetchone()
    models = {
        r["model"]: r["n"]
        for r in q("SELECT model, SUM(attempts) AS n FROM candidates WHERE attempts > 0 GROUP BY model")
    }
    clips = q(
        "SELECT COUNT(*) AS n, COALESCE(SUM(posted),0) AS posted FROM clips WHERE status = 'ok'"
    ).fetchone()
    return {
        "schema_version": store.version,
        "replays": _counts(store, "replays", "status"),
        "candidates": _counts(store, "candidates", "judge_status"),
        "keeps": q("SELECT COUNT(*) FROM candidates WHERE keep = 1").fetchone()[0],
        "clips": {"rendered": clips["n"], "posted": clips["posted"]},
        "judge": {"calls": judge["calls"], "cost_usd": round(judge["cost"], 4), "by_model": models},
        "labels": _counts(store, "labels", "label"),
        "stages": {s: stages[s] for s in STAGES if s in stages},
    }


def format_report(r: dict[str, Any]) -> str:
    def fmt(d: dict[str, int]) -> str:
        return ", ".join(f"{v} {k}" for k, v in sorted(d.items())) or "none"

    lines = [
        f"replays:    {sum(r['replays'].values())} ({fmt(r['replays'])})",
        f"candidates: {sum(r['candidates'].values())} ({fmt(r['candidates'])})",
        f"keeps:      {r['keeps']}   clips rendered: {r['clips']['rendered']}   posted: {r['clips']['posted']}",  # noqa: E501
        f"judge:      {r['judge']['calls']} calls, notional ${r['judge']['cost_usd']:.4f} ({fmt(r['judge']['by_model'])})",  # noqa: E501
        f"labels:     {fmt(r['labels'])}",
        "stage timings (avg s per replay, total s, replays):",
    ]  # fmt: skip
    lines += [
        f"  {name:<11} {st['avg_s']:>8.2f} {st['total_s']:>9.1f} {st['n']:>5}"
        for name, st in r["stages"].items()
    ]
    return "\n".join(lines)
