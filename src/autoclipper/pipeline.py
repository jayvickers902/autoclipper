"""Replay -> clips: probe, dead check, signals, candidates, judge, render. Idempotent and resumable.

Every stage is recorded in the store (status and seconds), so re-running a replay skips finished stages and
resumes at the first unfinished one. Candidates the judge could not rate because Claude was paused stay
`queued` (replay status `queued`); the next pass re-judges them. Reads originals only; every write goes
through `assert_writable`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import media, signals
from .candidates import Candidate, find_candidates
from .config import Settings
from .gpu_gate import GpuGate
from .judging import Judge, JudgeOutcome
from .safety import assert_writable
from .scan import StabilityTracker, candidate_files, file_sha256, settle
from .signals.tagger import PannsTagger
from .store import TERMINAL, Store

STAGES = ("probe", "dead_check", "analyze", "candidates", "judge", "render")
WEBP_SECONDS = 3.0


@dataclass(frozen=True)
class Options:
    whisper: bool = True
    tags: bool = False  # AudioSet tags (PANNs, CPU ONNX); opt-in: --tags / AUTOCLIPPER_TAGS=1
    max_candidates: int | None = None
    min_score: float = 0.6
    retry_failed: bool = True  # explicit `process` retries; watch/backfill only with --retry-failed
    encoder: str = "auto"
    max_mb: float = 19.0


@dataclass
class Context:
    settings: Settings
    store: Store
    judge: Judge
    options: Options = field(default_factory=Options)
    log: Callable[[str], None] = lambda _msg: None
    gpu_gate: GpuGate | None = None  # set by `run`: encodes go to the CPU while a game is running
    should_stop: Callable[[], bool] = lambda: False  # checked between replays (SIGTERM in `run`)


@dataclass
class ReplayResult:
    path: Path
    status: str  # the replay's status after this run (or "skipped"/"unstable")
    replay_id: int | None = None
    skipped: bool = False
    candidates: int = 0
    keeps: int = 0
    clips: int = 0
    queued: int = 0
    failed: int = 0
    note: str = ""


def _timed(ctx: Context, rid: int, stage: str, fn: Callable[[], Any]) -> Any:
    t0 = time.perf_counter()
    try:
        out = fn()
    except Exception as exc:
        ctx.store.record_stage(rid, stage, time.perf_counter() - t0, "failed", f"{type(exc).__name__}: {exc}")
        raise
    dt = time.perf_counter() - t0
    ctx.store.record_stage(rid, stage, dt)
    ctx.log(f"  {stage}: {dt:.1f}s")
    return out


def register(ctx: Context, path: Path) -> dict[str, Any]:
    """The replay's store row, hashing the file only if its (path, size, mtime) is new. May be a duplicate."""
    st = path.stat()
    known = ctx.store.replay_by_stat(path, st.st_size, st.st_mtime)
    sha = known["sha256"] if known else file_sha256(path)
    row = ctx.store.upsert_replay(path, sha, st.st_size, st.st_mtime)
    if row["status"] == "new":
        original = ctx.store.original_of(sha, path)
        if original is not None:
            ctx.store.update_replay(row["id"], status="duplicate", duplicate_of=original["id"])
            row = ctx.store.replay(row["id"])
    return row


def needs_work(ctx: Context, path: Path) -> bool:
    """Cheap pre-filter for passes: False if the store already shows this exact file as finished."""
    try:
        st = path.stat()
    except OSError:
        return False
    row = ctx.store.replay_by_stat(path, st.st_size, st.st_mtime)
    if row is None:
        return True
    if row["status"] in TERMINAL:
        return False
    return not (row["status"] == "failed" and not ctx.options.retry_failed)


def process_replay(ctx: Context, path: Path | str) -> ReplayResult:
    p = Path(path)
    row = register(ctx, p)
    rid = row["id"]
    res = ReplayResult(p, row["status"], rid)
    if row["status"] in TERMINAL or (row["status"] == "failed" and not ctx.options.retry_failed):
        res.skipped = True
        res.note = f"already {row['status']}"
        return res
    ctx.log(f"{p.name}: processing")
    try:
        _run(ctx, p, rid, res)
    except Exception as exc:  # a broken replay must not stop a backfill; the error is on the row
        msg = f"{type(exc).__name__}: {exc}"[:500]
        ctx.store.update_replay(rid, status="failed", error=msg)
        res.status, res.note = "failed", msg
        ctx.log(f"  FAILED {msg}")
    return res


def _run(ctx: Context, p: Path, rid: int, res: ReplayResult) -> None:
    store, opts = ctx.store, ctx.options
    if ctx.options.retry_failed:
        store.reset_failed_candidates(rid)
    store.update_replay(rid, status="new", error="")

    if not store.stage_done(rid, "probe"):
        info = _timed(ctx, rid, "probe", lambda: media.probe(p))
        store.update_replay(rid, duration=info.duration)

    if not store.stage_done(rid, "dead_check"):
        dead, reason = _timed(ctx, rid, "dead_check", lambda: media.is_dead_capture(p))
        store.update_replay(rid, dead=int(dead), dead_reason=reason if dead else "")
    row = store.replay(rid)
    if row["dead"]:
        store.update_replay(rid, status="dead")
        res.status, res.note = "dead", row["dead_reason"]
        ctx.log(f"  dead capture: {row['dead_reason']}")
        return

    timeline = _timeline(ctx, p, rid, row)
    if not store.stage_done(rid, "candidates"):
        cands = _timed(
            ctx, rid, "candidates",
            lambda: find_candidates(timeline, max_n=opts.max_candidates, min_score=opts.min_score),
        )  # fmt: skip
        store.add_candidates(rid, [c.to_dict() for c in cands])

    _judge(ctx, p, rid, timeline)
    _render_keeps(ctx, p, rid)

    cands = store.candidates(rid)
    res.candidates = len(cands)
    res.keeps = sum(1 for c in cands if c["keep"])
    res.queued = sum(1 for c in cands if c["judge_status"] == "queued")
    res.failed = sum(1 for c in cands if c["judge_status"] == "failed")
    clips = [store.clip_for(c["id"]) for c in cands if c["keep"]]
    res.clips = sum(1 for c in clips if c and c["status"] == "ok")
    render_failed = sum(1 for c in clips if c is None or c["status"] != "ok")
    if res.queued:
        status, note = "queued", f"{res.queued} candidate(s) waiting for Claude"
    elif res.failed or render_failed:
        status, note = "failed", f"{res.failed} judge failure(s), {render_failed} render failure(s)"
        store.update_replay(rid, error=note)
    else:
        status, note = "done", ""
    store.update_replay(rid, status=status)
    res.status, res.note = status, note
    ctx.log(f"  {status}: {res.candidates} candidates, {res.keeps} keeps, {res.clips} clips {note}".rstrip())


_TAGGER: PannsTagger | None = None


def _tagger() -> PannsTagger:
    """One shared tagger per process (the ONNX session is loaded on first use)."""
    global _TAGGER
    _TAGGER = _TAGGER or PannsTagger()
    return _TAGGER


def _timeline(ctx: Context, p: Path, rid: int, row: dict) -> dict:
    """Load the saved timeline, or build and save it (analyze stage)."""
    path = Path(row["timeline_path"]) if row["timeline_path"] else None
    if ctx.store.stage_done(rid, "analyze") and path and path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    dest = assert_writable(
        ctx.settings.data_dir / "timelines" / f"{p.stem}-{row['sha256'][:8]}.json", ctx.settings
    )

    def build() -> dict:
        tagger = _tagger() if ctx.options.tags else None
        tl = signals.analyze(p, whisper=ctx.options.whisper, check_dead=False, tagger=tagger)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(tl, indent=1) + "\n", encoding="utf-8")
        tmp.replace(dest)
        return tl

    timeline = _timed(ctx, rid, "analyze", build)
    ctx.store.update_replay(rid, timeline_path=str(dest))
    return timeline


def _judge(ctx: Context, p: Path, rid: int, timeline: dict) -> None:
    todo = ctx.store.candidates(rid, ("pending", "queued"))
    if not todo:
        if not ctx.store.stage_done(rid, "judge"):
            ctx.store.record_stage(rid, "judge", 0.0)
        return
    paused_fn = getattr(ctx.judge, "paused", None)
    paused = paused_fn() if callable(paused_fn) else None

    def run() -> None:
        nonlocal paused
        for c in todo:
            if paused:
                ctx.store.set_judgement(c["id"], "queued", error=paused, called=False)
                continue
            try:
                out: JudgeOutcome = ctx.judge(p, Candidate(**c["data"]), timeline)
            except Exception as exc:
                ctx.store.set_judgement(c["id"], "failed", error=f"{type(exc).__name__}: {exc}")
                continue
            if out.queued:
                paused = out.error or "judge unavailable"  # the pause is global: stop calling for this pass
                ctx.store.set_judgement(
                    c["id"], "queued", model=out.model, cost_usd=out.cost_usd, error=paused
                )
            elif out.error:
                ctx.store.set_judgement(
                    c["id"], "failed", model=out.model, cost_usd=out.cost_usd, error=out.error
                )
            else:
                ctx.store.set_judgement(
                    c["id"], "judged", verdict=out.verdict, model=out.model or out.source, cost_usd=out.cost_usd  # noqa: E501
                )  # fmt: skip

    _timed(ctx, rid, "judge", run)


def _clamp(v: dict, c: dict) -> tuple[float, float, float]:
    """Verdict start/end/thumb_t, falling back to the candidate window when the verdict is unusable."""
    start, end = float(v.get("start", c["start"])), float(v.get("end", c["end"]))
    if end <= start:
        start, end = c["start"], c["end"]
    thumb = float(v.get("thumb_t", (start + end) / 2))
    return start, end, min(max(thumb, start), end)


def _render_keeps(ctx: Context, p: Path, rid: int) -> None:
    todo = []
    for c in ctx.store.candidates(rid, ("judged",)):
        clip = ctx.store.clip_for(c["id"])
        if c["keep"] and not (clip and clip["status"] == "ok" and _files_exist(clip)):
            todo.append(c)
    if not todo and ctx.store.stage_done(rid, "render"):
        return

    def run() -> None:
        for c in todo:
            try:
                render_clip(ctx, p, c)
            except Exception as exc:
                ctx.store.save_clip(c["id"], status="failed", error=f"{type(exc).__name__}: {exc}"[:500])

    _timed(ctx, rid, "render", run)


def _files_exist(clip: dict) -> bool:
    return all(Path(clip[k]).is_file() for k in ("cut_path", "share_path", "thumb_path", "webp_path"))


def render_clip(ctx: Context, replay: Path, cand: dict) -> int:
    """cut + share encode + thumbnail + webp into output_dir/<replay-stem>/; returns the clip id."""
    s = ctx.settings
    start, end, thumb_t = _clamp(cand["verdict"] or {}, cand)
    out_dir = assert_writable(s.output_dir / replay.stem, s)
    base = f"clip{cand['idx'] + 1:02d}_{int(start):04d}s"
    paths = {
        "cut": assert_writable(out_dir / f"{base}_cut.mp4", s),
        "share": assert_writable(out_dir / f"{base}_share.mp4", s),
        "thumb": assert_writable(out_dir / f"{base}_thumb.jpg", s),
        "webp": assert_writable(out_dir / f"{base}_preview.webp", s),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    media.cut_copy(replay, start, end, paths["cut"])
    encoder = ctx.gpu_gate.encoder(ctx.options.encoder) if ctx.gpu_gate else ctx.options.encoder
    enc = media.share_encode(replay, start, end, paths["share"], max_mb=ctx.options.max_mb, encoder=encoder)
    media.thumbnail(replay, thumb_t, paths["thumb"])
    webp_start = min(max(thumb_t - WEBP_SECONDS / 2, start), max(start, end - WEBP_SECONDS))
    media.preview_webp(replay, webp_start, paths["webp"], seconds=WEBP_SECONDS)
    ctx.log(f"  clip {base}: {enc.size_bytes / 1e6:.1f} MB ({enc.encoder})")
    return ctx.store.save_clip(
        cand["id"], status="ok", out_dir=str(out_dir), cut_path=str(paths["cut"]), share_path=str(paths["share"]),  # noqa: E501
        thumb_path=str(paths["thumb"]), webp_path=str(paths["webp"]), share_bytes=enc.size_bytes,
        encoder=enc.encoder, error="",
    )  # fmt: skip


def process_pass(
    ctx: Context,
    directory: Path | None = None,
    *,
    tracker: StabilityTracker | None = None,
    limit: int | None = None,
    stable_wait: float = 2.0,
) -> list[ReplayResult]:
    """One scan of `directory` (default replay_dir). Process up to `limit` replays that need work.

    With a `tracker` (watch loop) a file must have been seen unchanged on an earlier pass; without one the
    pass waits `stable_wait` s and checks twice itself. Queued replays are skipped while the judge is paused.
    """
    s = ctx.settings
    files = [f for f in candidate_files(directory or s.replay_dir, s.output_dir) if needs_work(ctx, f)]
    stable = tracker.observe(files) if tracker else settle(files, stable_wait)
    results: list[ReplayResult] = []
    paused_fn = getattr(ctx.judge, "paused", None)
    paused = paused_fn() if callable(paused_fn) else None
    for f in files:
        if (limit is not None and len(results) >= limit) or ctx.should_stop():
            break
        if f not in stable:
            continue
        st = f.stat()
        row = ctx.store.replay_by_stat(f, st.st_size, st.st_mtime)
        if paused and row and row["status"] == "queued":
            ctx.log(f"{f.name}: queued, Claude paused ({paused})")
            continue
        results.append(process_replay(ctx, f))
    return results
