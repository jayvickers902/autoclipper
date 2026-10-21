"""Judge comparison (P2.9): Haiku alone, Haiku + Sonnet rejudge of borderline verdicts, and the local ollama
judge, scored against a Sonnet reference or against Jay's labels from the review page.

    uv run python -m autoclipper.judge.compare run [--reference 17] [--rejudge 11] [--haiku-repeat 0]
        [--ollama] [--ollama-vision 0] [--max-sonnet 30] [--max-haiku 20]
    uv run python -m autoclipper.judge.compare report [--against sonnet|labels]

The Haiku verdicts are the ones already in the pipeline store (read-only). Every new answer is stored in
data/judge-compare.sqlite, so `run` only makes the calls still missing and `report` makes none. Claude calls
go through the guarded path (Guard slot + ledger row, one attempt each, role "polish" for Sonnet so they count
against the daily Sonnet cap too); --max-sonnet / --max-haiku cap this tool's own calls over all its runs.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import sqlite3
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path

from ..config import Settings
from .core import JudgeResult, _claude, _ctx
from .guard import Guard
from .ollama import ollama_judge
from .prompt import SYSTEM_JUDGE, SYSTEM_REJUDGE, prior_verdict_text
from .schema import SchemaError, Verdict, is_borderline, validate

DB_NAME = "judge-compare.sqlite"
SONNET_JUDGES = ("sonnet", "rejudge")
HAIKU_JUDGES = ("haiku2",)
SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    candidate_id INTEGER NOT NULL,
    judge TEXT NOT NULL,          -- sonnet (reference) / rejudge / haiku2 (repeat) / ollama / ollama-vision
    model TEXT NOT NULL DEFAULT '',
    ok INTEGER NOT NULL,
    verdict TEXT,
    error TEXT NOT NULL DEFAULT '',
    calls INTEGER NOT NULL DEFAULT 0,   -- Claude calls spent on this row over all runs (budget)
    duration_s REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    ledger_rows TEXT NOT NULL DEFAULT '[]',
    ts REAL NOT NULL,
    PRIMARY KEY (candidate_id, judge)
);
"""


# --- data ------------------------------------------------------------------------------------------------
def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_candidates(store_db: Path) -> tuple[list[dict], dict[int, str]]:
    """Judged candidates with their Haiku verdict, and the latest label per candidate (read-only)."""
    con = _ro(store_db)
    try:
        rows = con.execute(
            """SELECT c.id, c.data, c.verdict, c.model, r.path, r.timeline_path FROM candidates c
               JOIN replays r ON r.id = c.replay_id
               WHERE c.judge_status = 'judged' AND c.verdict IS NOT NULL ORDER BY c.id"""
        ).fetchall()
        labels = {r[0]: r[1] for r in con.execute("SELECT candidate_id, label FROM labels ORDER BY id")}
    finally:
        con.close()
    cands = [
        {
            "id": r["id"],
            "cand": json.loads(r["data"]),
            "haiku": json.loads(r["verdict"]),
            "model": r["model"],
            "path": r["path"],
            "timeline_path": r["timeline_path"],
        }
        for r in rows
    ]
    return cands, labels


class CompareDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)

    def row(self, cid: int, judge: str) -> sqlite3.Row | None:
        return self.con.execute(
            "SELECT * FROM verdicts WHERE candidate_id=? AND judge=?", (cid, judge)
        ).fetchone()

    def calls(self, judges: tuple[str, ...]) -> int:
        q = f"SELECT COALESCE(SUM(calls),0) FROM verdicts WHERE judge IN ({','.join('?' * len(judges))})"
        return int(self.con.execute(q, judges).fetchone()[0])

    def save(
        self,
        cid: int,
        judge: str,
        model: str,
        data: dict | None,
        error: str,
        calls: int = 0,
        duration_s: float | None = None,
        tokens: tuple[int, int] | None = None,
        ledger_rows: list[int] | None = None,
    ) -> None:
        prev = self.row(cid, judge)
        rows = (json.loads(prev["ledger_rows"]) if prev else []) + (ledger_rows or [])
        with self.con:
            self.con.execute(
                "INSERT OR REPLACE INTO verdicts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cid,
                    judge,
                    model,
                    int(data is not None),
                    json.dumps(data) if data else None,
                    error[:500],
                    (prev["calls"] if prev else 0) + calls,
                    duration_s,
                    tokens[0] if tokens else None,
                    tokens[1] if tokens else None,
                    json.dumps(rows),
                    time.time(),
                ),
            )

    def verdicts(self, judge: str) -> dict[int, dict]:
        rows = self.con.execute("SELECT candidate_id, verdict FROM verdicts WHERE judge=? AND ok=1", (judge,))
        return {r[0]: json.loads(r[1]) for r in rows}

    def stats_rows(self, judge: str) -> list[sqlite3.Row]:
        return self.con.execute("SELECT * FROM verdicts WHERE judge=? AND ok=1", (judge,)).fetchall()


# --- sample ----------------------------------------------------------------------------------------------
def pick_sample(cands: list[dict], n: int) -> list[dict]:
    """Stratify by (Tarkov or not, Haiku keep), spread over Haiku scores within each stratum; strata are
    interleaved so a prefix of the result is still balanced."""
    strata: dict[tuple[bool, bool], list[dict]] = {}
    for c in cands:
        v = c["haiku"]
        strata.setdefault((v.get("game") == "tarkov", bool(v.get("keep"))), []).append(c)
    keys = sorted(strata)
    want = dict.fromkeys(keys, 0)
    while sum(want.values()) < min(n, len(cands)):
        for k in sorted(keys, key=lambda k: (want[k], -len(strata[k]))):
            if want[k] < len(strata[k]):
                want[k] += 1
                break
    picked: dict[tuple[bool, bool], list[dict]] = {}
    for k in keys:
        items = sorted(strata[k], key=lambda c: (c["haiku"].get("score", 0), c["id"]))
        m = want[k]
        picked[k] = [items[int((i + 0.5) * len(items) / m)] for i in range(m)] if m else []
    out: list[dict] = []
    for i in range(max(want.values(), default=0)):
        out += [picked[k][i] for k in keys if i < len(picked[k])]
    return out


def borderline(v: dict) -> bool:
    return is_borderline(Verdict(score=int(v.get("score", 0))))


# --- run -------------------------------------------------------------------------------------------------
def _ledger_durations(ledger: Path, ids: list[int]) -> list[float]:
    if not ids or not ledger.exists():
        return []
    con = _ro(ledger)
    try:
        q = f"SELECT duration_s FROM calls WHERE id IN ({','.join('?' * len(ids))})"
        return [r[0] for r in con.execute(q, ids) if r[0] is not None]
    finally:
        con.close()


class Runner:
    def __init__(self, s: Settings, db: CompareDB, log: Callable[[str], None] = print):
        self.s, self.db, self.log = s, db, log
        self.guard = Guard(s)
        self.stopped: set[str] = set()  # families/judges that hit a refusal or budget this run

    def _ctx(self, c: dict, judge: str):
        timeline = json.loads(Path(c["timeline_path"]).read_text())
        return _ctx(c["path"], c["cand"], timeline, self.s, f"compare:{judge}:{c['id']}", self.guard, None)

    def claude(
        self, c: dict, judge: str, role: str, model: str, system: str, extra: str, budget: int
    ) -> None:
        fam = "sonnet" if judge in SONNET_JUDGES else "haiku"
        prev = self.db.row(c["id"], judge)
        if fam in self.stopped or (prev and prev["ok"]):
            return
        used = self.db.calls(SONNET_JUDGES if fam == "sonnet" else HAIKU_JUDGES)
        if used >= budget:
            self.log(f"{fam} budget for this comparison used up ({used}/{budget})")
            self.stopped.add(fam)
            return
        out = JudgeResult(verdict=Verdict())
        try:
            data = _claude(self._ctx(c, judge), out, role, model, system, extra, attempts=1)
        except Exception as e:  # unreadable replay or timeline: record it, move on
            self.db.save(c["id"], judge, model, None, f"{type(e).__name__}: {e}")
            self.log(f"{judge} {c['id']}: {type(e).__name__}: {e}")
            return
        if not out.ledger_rows:  # refused by the guard: no call was made
            self.log(f"{judge} {c['id']}: {'; '.join(out.attempts)}")
            self.stopped.add(fam)
            return
        dur = _ledger_durations(Path(self.s.ledger_path), out.ledger_rows)
        self.db.save(
            c["id"],
            judge,
            model,
            data,
            "; ".join(out.attempts),
            len(out.ledger_rows),
            sum(dur) if dur else None,
            (out.input_tokens, out.output_tokens),
            out.ledger_rows,
        )
        self.log(
            f"{judge} {c['id']}: " + (f"keep={data['keep']} score={data['score']}" if data else "failed")
        )

    def ollama(self, c: dict, vision: bool, gate: Callable[[], bool]) -> None:
        judge = "ollama-vision" if vision else "ollama"
        prev = self.db.row(c["id"], judge)
        if judge in self.stopped or (prev and prev["ok"]):
            return
        if gate():
            self.log("a game is running: no ollama (GPU) now")
            self.stopped.add(judge)
            return
        s = dataclasses.replace(self.s, ollama_vision=vision)
        try:
            ctx = self._ctx(c, judge)
            images = [b for _, b in ctx.frames()] if vision else []
        except Exception as e:
            self.db.save(c["id"], judge, "", None, f"{type(e).__name__}: {e}")
            return
        o = ollama_judge(s, SYSTEM_JUDGE, ctx.text, images, ctx.schema)
        if o.unavailable:
            self.log(f"{judge}: {o.error}")
            self.stopped.add(judge)
            return
        data, err = None, o.error
        if o.ok:
            try:
                data = validate(o.data, ctx.start, ctx.end)
            except SchemaError as e:
                err = f"schema: {e}"
        self.db.save(c["id"], judge, o.model, data, err, duration_s=o.duration_s)
        self.log(
            f"{judge} {c['id']}: " + (f"keep={data['keep']} score={data['score']}" if data else err[:120])
        )


def cmd_run(a: argparse.Namespace, s: Settings) -> int:
    cands, _ = load_candidates(Path(s.data_dir) / "autoclipper.sqlite")
    db = CompareDB(Path(s.data_dir) / DB_NAME)
    r = Runner(s, db)
    sample = pick_sample(cands, a.reference)
    print(f"{len(cands)} judged candidates; reference sample {[c['id'] for c in sample]}")
    for c in sample:
        r.claude(c, "sonnet", "polish", s.polish_model, SYSTEM_JUDGE, "", a.max_sonnet)
    for c in [c for c in sample if borderline(c["haiku"])][: a.rejudge]:
        extra = prior_verdict_text(c["haiku"], c["model"] or s.judge_model)
        r.claude(c, "rejudge", "polish", s.polish_model, SYSTEM_REJUDGE, extra, a.max_sonnet)
    for c in sample[: a.haiku_repeat]:
        r.claude(c, "haiku2", "judge", s.judge_model, SYSTEM_JUDGE, "", a.max_haiku)
    if a.ollama or a.ollama_vision:
        from ..gpu_gate import GpuGate

        gate = GpuGate().games_running
        if a.ollama:
            for c in cands:
                r.ollama(c, False, gate)
        for c in sample[: a.ollama_vision]:
            r.ollama(c, True, gate)
    print(f"comparison calls so far: sonnet {db.calls(SONNET_JUDGES)}, haiku {db.calls(HAIKU_JUDGES)}")
    return 0


# --- metrics ---------------------------------------------------------------------------------------------
def _pearson(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3 or len(set(x)) < 2 or len(set(y)) < 2:
        return None
    return round(statistics.correlation(x, y), 3)


def _ranks(x: list[float]) -> list[float]:
    order = sorted(range(len(x)), key=lambda i: x[i])
    r = [0.0] * len(x)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def agreement(judge: dict[int, dict], ref: dict[int, dict]) -> dict:
    """Keep/drop agreement (keep = positive), Cohen's kappa, score correlation, game/type agreement."""
    ids = sorted(set(judge) & set(ref))
    n = len(ids)
    if not n:
        return {"n": 0}
    jk = [bool(judge[i]["keep"]) for i in ids]
    rk = [bool(ref[i]["keep"]) for i in ids]
    tp = sum(a and b for a, b in zip(jk, rk, strict=True))
    tn = sum(not a and not b for a, b in zip(jk, rk, strict=True))
    fp = sum(a and not b for a, b in zip(jk, rk, strict=True))
    fn = n - tp - tn - fp
    po = (tp + tn) / n
    pj, pr = sum(jk) / n, sum(rk) / n
    pe = pj * pr + (1 - pj) * (1 - pr)
    kappa = (po - pe) / (1 - pe) if pe < 1 else (1.0 if po == 1 else 0.0)
    out: dict = {
        "n": n,
        "accuracy": round(po, 3),
        "kappa": round(kappa, 3),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "keep_rate": round(pj, 3),
        "ref_keep_rate": round(pr, 3),
    }
    scored = [i for i in ids if "score" in ref[i] and "score" in judge[i]]
    if scored:
        x = [float(judge[i]["score"]) for i in scored]
        y = [float(ref[i]["score"]) for i in scored]
        out["score_pearson"] = _pearson(x, y)
        out["score_spearman"] = _pearson(_ranks(x), _ranks(y))
        out["score_mae"] = round(sum(abs(a - b) for a, b in zip(x, y, strict=True)) / len(x), 2)
        out["score_mean"] = round(statistics.fmean(x), 2)
        out["ref_score_mean"] = round(statistics.fmean(y), 2)
    for f in ("game", "type"):
        both = [i for i in ids if f in ref[i] and f in judge[i]]
        if both:
            out[f"{f}_agree"] = round(sum(judge[i][f] == ref[i][f] for i in both) / len(both), 3)
    both = [i for i in ids if "game" in ref[i] and "game" in judge[i]]
    if both:  # the backfill schema had no cs2, so also score with cs2 folded into other
        fold = {"cs2": "other"}
        same = sum(
            fold.get(judge[i]["game"], judge[i]["game"]) == fold.get(ref[i]["game"], ref[i]["game"])
            for i in both
        )
        out["game_agree_cs2_as_other"] = round(same / len(both), 3)
    return out


def threshold_sweep(judge: dict[int, dict], ref: dict[int, dict]) -> dict[int, float]:
    """Accuracy vs ref.keep if keep were `score >= t` instead of the model's own keep."""
    ids = sorted(set(judge) & set(ref))
    return (
        {
            t: round(sum((judge[i]["score"] >= t) == bool(ref[i]["keep"]) for i in ids) / len(ids), 3)
            for t in range(3, 10)
        }
        if ids
        else {}
    )


def combine_rejudge(haiku: dict[int, dict], rejudge: dict[int, dict]) -> dict[int, dict]:
    """Mode (b): Haiku's verdict, replaced by the Sonnet rejudge where Haiku was borderline. Borderline
    candidates without a rejudge are left out (unknown)."""
    return {
        i: rejudge[i] if borderline(v) else v for i, v in haiku.items() if not borderline(v) or i in rejudge
    }


def _cost(rows: list[dict]) -> dict:
    def med(xs: list[float]) -> float | None:
        return round(statistics.median(xs), 2) if xs else None

    def mean(xs: list[float]) -> float | None:
        return round(statistics.fmean(xs), 1) if xs else None

    d = [r["duration_s"] for r in rows if r.get("duration_s") is not None]
    i = [r["input_tokens"] for r in rows if r.get("input_tokens") is not None]
    o = [r["output_tokens"] for r in rows if r.get("output_tokens") is not None]
    return {
        "calls": len(rows),
        "mean_s": mean(d),
        "median_s": med(d),
        "p90_s": (round(sorted(d)[min(len(d) - 1, math.ceil(0.9 * len(d)) - 1)], 2) if d else None),
        "input_tokens": mean(i),
        "output_tokens": mean(o),
    }


def _haiku_cost(ledger: Path, cands: list[dict]) -> dict:
    """Latency/tokens of the original Haiku verdicts, from their ledger rows (read-only)."""
    if not ledger.exists():
        return _cost([])
    want = {c["haiku"].get("candidate_id") for c in cands}
    con = _ro(ledger)
    try:
        rows = con.execute(
            """SELECT candidate_id, duration_s, COALESCE(input_tokens,0) + COALESCE(cache_read_tokens,0)
               + COALESCE(cache_creation_tokens,0) AS input_tokens, output_tokens
               FROM calls WHERE role='judge' AND status='ok' AND model LIKE '%haiku%' ORDER BY id"""
        ).fetchall()
    finally:
        con.close()
    first: dict[str, dict] = {}
    for r in rows:
        if r["candidate_id"] in want and r["candidate_id"] not in first:
            first[r["candidate_id"]] = dict(r)
    return _cost(list(first.values()))


def build_report(s: Settings, against: str) -> dict:
    cands, labels = load_candidates(Path(s.data_dir) / "autoclipper.sqlite")
    db = CompareDB(Path(s.data_dir) / DB_NAME)
    haiku = {c["id"]: c["haiku"] for c in cands}
    judges = {
        "haiku": haiku,
        "haiku+rejudge": combine_rejudge(haiku, db.verdicts("rejudge")),
        "ollama": db.verdicts("ollama"),
        "ollama-vision": db.verdicts("ollama-vision"),
        "haiku-repeat": db.verdicts("haiku2"),
        "sonnet": db.verdicts("sonnet"),
    }
    if against == "labels":
        ref = {i: {"keep": lab == "up"} for i, lab in labels.items() if i in haiku}
    else:
        ref = judges.pop("sonnet")
    n_border = sum(borderline(v) for v in haiku.values())
    report = {
        "against": against,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "candidates": len(cands),
        "reference_n": len(ref),
        "agreement": {k: agreement(v, ref) for k, v in judges.items() if v},
        "haiku_self_agreement": agreement(judges["haiku-repeat"], haiku) if judges["haiku-repeat"] else None,
        "threshold_sweep": {k: threshold_sweep(judges[k], ref) for k in ("haiku", "ollama") if judges[k]},
        "borderline": {
            "haiku_borderline": n_border,
            "sonnet_calls_per_candidate": round(n_border / len(cands), 3) if cands else None,
        },
        "cost": {
            "haiku": _haiku_cost(Path(s.ledger_path), cands),
            **{
                j: _cost([dict(r) for r in db.stats_rows(j)])
                for j in ("sonnet", "rejudge", "haiku2", "ollama", "ollama-vision")
            },
        },
        "comparison_calls": {"sonnet": db.calls(SONNET_JUDGES), "haiku": db.calls(HAIKU_JUDGES)},
        "per_candidate": [
            {
                "id": c["id"],
                "replay": Path(c["path"]).stem.removeprefix("Replay "),
                "window": [c["cand"]["start"], c["cand"]["end"]],
                "label": labels.get(c["id"]),
                **{
                    k: {f: v[c["id"]].get(f) for f in ("keep", "score", "game", "type")}
                    for k, v in {**judges, **({"sonnet": ref} if against == "sonnet" else {})}.items()
                    if c["id"] in v
                },
            }
            for c in cands
        ],
    }
    return report


def _fmt(x: object) -> str:
    return "–" if x is None else f"{x:.2f}" if isinstance(x, float) else str(x)


def markdown(rep: dict) -> str:
    ref = "Sonnet" if rep["against"] == "sonnet" else "Jay's labels"
    lines = [
        f"Reference: {ref}, n = {rep['reference_n']}.",
        "",
        "| Judge | n | Accuracy | Kappa | TP/FP/FN/TN | Keep rate (ref) | Score r (Spearman) | Score MAE "
        "| Game | Game, cs2=other | Type |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for k, m in rep["agreement"].items():
        if not m.get("n"):
            continue
        cf, f = m["confusion"], _fmt
        lines.append(
            f"| {k} | {m['n']} | {f(m['accuracy'])} | {f(m['kappa'])} | "
            f"{cf['tp']}/{cf['fp']}/{cf['fn']}/{cf['tn']} | {f(m['keep_rate'])} ({f(m['ref_keep_rate'])}) | "
            f"{f(m.get('score_pearson'))} ({f(m.get('score_spearman'))}) | {f(m.get('score_mae'))} | "
            f"{f(m.get('game_agree'))} | {f(m.get('game_agree_cs2_as_other'))} | {f(m.get('type_agree'))} |"
        )
    lines += [
        "",
        "| Judge | Calls | Mean s | Median s | p90 s | Input tokens | Output tokens |",
        "|---|---|---|---|---|---|---|",
    ]
    for k, c in rep["cost"].items():
        if c["calls"]:
            lines.append(
                f"| {k} | {c['calls']} | {_fmt(c['mean_s'])} | {_fmt(c['median_s'])} | "
                f"{_fmt(c['p90_s'])} | {_fmt(c['input_tokens'])} | {_fmt(c['output_tokens'])} |"
            )
    if rep["threshold_sweep"]:
        ts = sorted({t for sw in rep["threshold_sweep"].values() for t in sw})
        lines += [
            "",
            "| keep = score >= t | " + " | ".join(str(t) for t in ts) + " |",
            "|---|" + "---|" * len(ts),
        ]
        for k, sw in rep["threshold_sweep"].items():
            lines.append(f"| {k} | " + " | ".join(_fmt(sw.get(t)) for t in ts) + " |")
    b = rep["borderline"]
    lines += [
        "",
        f"Borderline (score 4-6) Haiku verdicts: {b['haiku_borderline']} of {rep['candidates']} "
        f"→ {b['sonnet_calls_per_candidate']} Sonnet calls per candidate with rejudge on.",
    ]
    if rep.get("haiku_self_agreement"):
        h = rep["haiku_self_agreement"]
        lines.append(
            f"Haiku repeat vs original Haiku: n = {h['n']}, keep agreement {_fmt(h['accuracy'])}, "
            f"kappa {_fmt(h['kappa'])}, score MAE {_fmt(h.get('score_mae'))}."
        )
    return "\n".join(lines)


def cmd_report(a: argparse.Namespace, s: Settings) -> int:
    rep = build_report(s, a.against)
    out = Path(a.out) if a.out else Path(s.data_dir) / f"judge-compare-{a.against}.json"
    out.write_text(json.dumps(rep, indent=2))
    print(markdown(rep))
    print(f"\nwrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m autoclipper.judge.compare")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="make the comparison calls still missing (Claude calls are guarded)")
    r.add_argument("--reference", type=int, default=17, help="Sonnet reference sample size")
    r.add_argument("--rejudge", type=int, default=11, help="max borderline Sonnet rejudges in the sample")
    r.add_argument("--haiku-repeat", type=int, default=0, help="Haiku re-runs on the sample (variance)")
    r.add_argument("--ollama", action="store_true", help="local text judge on every candidate")
    r.add_argument("--ollama-vision", type=int, default=0, help="vision judge on the first N of the sample")
    r.add_argument("--max-sonnet", type=int, default=30, help="Sonnet calls this tool may make, all runs")
    r.add_argument("--max-haiku", type=int, default=20, help="Haiku calls this tool may make, all runs")
    rp = sub.add_parser("report", help="metrics from stored verdicts (no calls)")
    rp.add_argument("--against", choices=("sonnet", "labels"), default="sonnet")
    rp.add_argument("--out", help="results JSON (default data/judge-compare-<against>.json)")
    a = p.parse_args(argv)
    s = Settings.from_env()
    return cmd_run(a, s) if a.cmd == "run" else cmd_report(a, s)


if __name__ == "__main__":
    sys.exit(main())
