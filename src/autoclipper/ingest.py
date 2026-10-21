"""Long recordings -> clips: Segra session files and OBS (split) recordings, tens of minutes to hours.

    ingest(ctx, target, IngestOptions(...)) -> list[ReplayResult]     (`autoclipper ingest <file-or-dir>`)

Every file is a row in the store (`kind='long'`) with the usual stages, so a re-run skips what is finished:

    probe        duration, plus the wall-clock start (`started_at`) from the file name
                 (`2026-10-12 20-30-00`, Segra's `2026-10-12_20-30-00`), else Segra's CreatedAt - duration,
                 else mtime - duration (all naive local time, like the EFT logs)
    dead_check   `is_dead_sampled`: a few 30 s windows instead of decoding hours of video
    analyze      the audio in chunks of `chunk_s` seconds read with `overlap_s` of context on both sides;
                 each chunk is analysed like a short replay (bounded memory and time), cached on disk under
                 data_dir/chunks/, and only its own [start, end) is kept, so the stitched timeline has every
                 second exactly once. Chapter markers and Segra bookmarks are added as `hint` events.
    candidates   one timeline for the whole *group* (consecutive OBS split files are laid end to end, so a
                 fight across a split or a chunk boundary is one window); EFT raids from the logs become
                 segments, each with a candidate budget (one per SECONDS_PER_CANDIDATE, at least one);
                 hinted windows are always kept; candidates carry `map:<name>` and go back to the file that
                 holds their peak, in that file's own seconds
    judge/render as `process` (pipeline.judge_and_render)

Reads originals only; every write goes through `assert_writable` (store, timelines, chunk cache).
"""

from __future__ import annotations

import json
import math
import re
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from . import eft, hints, media, signals
from .candidates import MIN_LEN, Candidate, find_candidates, max_candidates
from .pipeline import (
    Context,
    ReplayResult,
    _tagger,
    _timed,
    judge_and_render,
    register,
)  # fmt: skip
from .safety import assert_writable
from .scan import settle
from .signals.analyze import VERSION as TIMELINE_VERSION
from .signals.analyze import assign_roles
from .store import TERMINAL
from .uploads import inbox_root

CHUNK_S = 300
OVERLAP_S = 10
SECONDS_PER_CANDIDATE = 240.0
SPLIT_GAP_S = 5.0  # consecutive files whose start/end differ by at most this are one split recording
RAID_EDGE_S = 3.0  # manual bookmarks this close to a raid start/end are the recorder's own raid markers
VIDEO_EXTS = (".mp4", ".mkv", ".mov")
_STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ _](\d{2})-(\d{2})-(\d{2})")


@dataclass(frozen=True)
class IngestOptions:
    eft_logs: Path | None = None
    chunk_s: int = CHUNK_S
    overlap_s: int = OVERLAP_S
    split_gap_s: float = SPLIT_GAP_S
    stable_wait: float = 2.0


@dataclass
class Rec:
    """One file of a group, with what the stages found."""

    path: Path
    row: dict
    res: ReplayResult
    duration: float = 0.0
    started: datetime | None = None
    offset: float = 0.0  # seconds from the group's start
    timeline: dict | None = None
    skip: bool = False  # terminal/dead/failed: no judge or render this run
    dead: bool = False
    hints: list[dict] = field(default_factory=list)


# --- finding files and groups -----------------------------------------------------------------------------


def list_recordings(target: Path, output_dir: Path) -> list[Path]:
    """`target` itself, or the video files under the directory `target` (recursive: Segra keeps sessions
    in `Full Sessions/<Game>/`), minus the output dir and OBS `Replay *` clips (`process` handles those)."""
    if target.is_file():
        return [target]
    out = output_dir.resolve(strict=False)
    files = [
        p for p in target.rglob("*")
        if p.suffix.lower() in VIDEO_EXTS and p.is_file() and not p.name.startswith("Replay ")
        and not p.resolve().is_relative_to(out)
    ]  # fmt: skip
    return sorted(files)


def name_time(path: Path) -> datetime | None:
    m = _STAMP.search(path.stem)
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups()))  # type: ignore[arg-type]
    except ValueError:
        return None


def _segra_created(meta: dict | None) -> datetime | None:
    raw = str((meta or {}).get("CreatedAt") or "")
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?", raw)
    return datetime.fromisoformat(m.group(1)) if m else None  # wall clock as written (offset dropped)


def started_at(path: Path, duration: float, meta: dict | None = None) -> tuple[datetime | None, str]:
    """(local wall clock of t=0, where it came from)."""
    t = name_time(path)
    if t:
        return t, "name"
    created = _segra_created(meta)  # Segra writes its metadata when the recording stops
    if created:
        return created - timedelta(seconds=duration), "segra"
    try:
        return datetime.fromtimestamp(path.stat().st_mtime) - timedelta(seconds=duration), "mtime"
    except OSError:
        return None, ""


def group_splits(recs: list[Rec], gap: float) -> list[list[Rec]]:
    """Consecutive files of one folder whose start follows the previous end within `gap` s form a group."""
    recs = sorted(recs, key=lambda r: (r.started or datetime.max, r.path.name))
    groups: list[list[Rec]] = []
    for r in recs:
        prev = groups[-1][-1] if groups else None
        if (
            prev is not None and prev.started and r.started and prev.path.parent == r.path.parent
            and abs((r.started - prev.started).total_seconds() - prev.duration) <= gap
        ):  # fmt: skip
            groups[-1].append(r)
        else:
            groups.append([r])
    return groups


# --- chunked analysis -------------------------------------------------------------------------------------


def chunk_plan(duration: float, chunk_s: int, overlap_s: int) -> list[tuple[int, float, int, float]]:
    """[(own_start, own_end, read_start, read_end)]: chunks own [k*chunk_s, (k+1)*chunk_s), the last one owns
    the rest; each is read with `overlap_s` of context either side (read_start is a whole second)."""
    n = max(1, math.ceil(duration / chunk_s))
    if n > 1 and duration - (n - 1) * chunk_s < overlap_s:
        n -= 1  # fold a sliver of a last chunk into the previous one
    out = []
    for k in range(n):
        own_a = k * chunk_s
        own_b = math.inf if k == n - 1 else (k + 1) * chunk_s
        read_a = max(0, own_a - overlap_s)
        read_b = duration if k == n - 1 else min(duration, own_b + overlap_s)
        out.append((own_a, own_b, read_a, read_b))
    return out


def stitch(chunks: list[tuple[int, float, int, dict]], duration: float, base: dict) -> dict:
    """Merge chunk timelines [(own_a, own_b, read_a, timeline)] into one in the file's seconds."""
    tl = {**base, "seconds": {}, "events": [], "transcript": []}
    for own_a, own_b, read_a, ct in chunks:
        for key in ("sources", "whisper_model", "whisper_device", "tagger"):
            if ct.get(key):
                tl[key] = ct[key]
        for role, entries in (ct.get("seconds") or {}).items():
            dest = tl["seconds"].setdefault(role, [])
            for e in entries:
                t = read_a + int(e["t"])
                if own_a <= t < own_b and t < math.ceil(duration):
                    dest.append({**e, "t": t})
        for e in ct.get("events") or []:
            t = read_a + float(e["t"])
            if own_a <= t < own_b:
                ev = {**e, "t": round(t, 3)}
                if "end" in e:
                    ev["end"] = round(read_a + float(e["end"]), 3)
                tl["events"].append(ev)
        for seg in ct.get("transcript") or []:
            if own_a <= read_a + float(seg["start"]) < own_b:
                tl["transcript"].append({
                    **seg, "start": round(read_a + seg["start"], 3), "end": round(read_a + seg["end"], 3),
                    "words": [{**w, "start": round(read_a + w["start"], 3),
                               "end": round(read_a + w["end"], 3)} for w in seg.get("words") or []],
                })  # fmt: skip
    for entries in tl["seconds"].values():
        entries.sort(key=lambda e: e["t"])
    tl["events"].sort(key=lambda e: e["t"])
    return tl


def analyze_chunked(ctx: Context, rec: Rec, iopts: IngestOptions) -> dict:
    """The file's signal timeline, built chunk by chunk (finished chunks are reused after a crash)."""
    s, opts = ctx.settings, ctx.options
    cache = assert_writable(s.data_dir / "chunks" / f"{rec.path.stem}-{rec.row['sha256'][:8]}", s)
    streams = media.audio_tracks(rec.path)
    roles = assign_roles(len(streams))
    base = {
        "version": TIMELINE_VERSION, "source": str(rec.path), "duration": round(rec.duration, 3),
        "dead": False, "dead_reason": "", "sources": {"transients": None, "speech": []},
        "tracks": [{"order": st.order, "index": st.index, "role": r}
                   for st, r in zip(streams, roles, strict=True)],
        "whisper_model": "", "whisper_device": "", "tagger": "",
        "chunks": {"size": iopts.chunk_s, "overlap": iopts.overlap_s},
    }  # fmt: skip
    parts = []
    plan = chunk_plan(rec.duration, iopts.chunk_s, iopts.overlap_s) if streams else []
    tagger = _tagger() if opts.tags else None
    for k, (own_a, own_b, read_a, read_b) in enumerate(plan):
        tag = f"{k:04d}_{read_a}-{read_b:.0f}_w{int(opts.whisper)}t{int(opts.tags)}.json"
        dest = assert_writable(cache / tag, s)
        if dest.is_file():
            ct = json.loads(dest.read_text(encoding="utf-8"))
        else:
            with tempfile.TemporaryDirectory(prefix="autoclipper-chunk-") as tmp:
                audio = media.extract_audio_window(rec.path, read_a, read_b - read_a, Path(tmp) / "chunk.mka")
                ct = signals.analyze(audio, whisper=opts.whisper, check_dead=False, tagger=tagger)
            cache.mkdir(parents=True, exist_ok=True)
            part = dest.with_suffix(".json.tmp")
            part.write_text(json.dumps(ct) + "\n", encoding="utf-8")
            part.replace(dest)
            ctx.log(f"  chunk {k + 1}/{len(plan)}: {read_a}-{read_b:.0f}s")
        parts.append((own_a, own_b, read_a, ct))
    tl = stitch(parts, rec.duration, base)
    tl["events"] = sorted(tl["events"] + rec.hints, key=lambda e: e["t"])
    return tl


def _timeline(ctx: Context, rec: Rec, iopts: IngestOptions) -> dict:
    rid, row = rec.row["id"], ctx.store.replay(rec.row["id"])
    path = Path(row["timeline_path"]) if row["timeline_path"] else None
    if ctx.store.stage_done(rid, "analyze") and path and path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    dest = assert_writable(
        ctx.settings.data_dir / "timelines" / f"{rec.path.stem}-{row['sha256'][:8]}.json", ctx.settings
    )

    def build() -> dict:
        tl = analyze_chunked(ctx, rec, iopts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(tl, indent=1) + "\n", encoding="utf-8")
        tmp.replace(dest)
        return tl

    tl = _timed(ctx, rid, "analyze", build)
    ctx.store.update_replay(rid, timeline_path=str(dest))
    return tl


# --- candidates over a group ------------------------------------------------------------------------------


def group_timeline(recs: list[Rec]) -> dict:
    """The group's files end to end (offsets in seconds); dead files leave a silent gap."""
    live = [r for r in recs if r.timeline]
    duration = recs[-1].offset + recs[-1].duration
    n = max(1, math.ceil(duration))
    first = live[0].timeline if live else {}
    roles = {role for r in live for role in (r.timeline.get("seconds") or {})}
    seconds = {role: [None] * n for role in roles}
    events: list[dict] = []
    transcript: list[dict] = []
    for r in live:
        tl, off = r.timeline, r.offset
        for role, entries in (tl.get("seconds") or {}).items():
            for e in entries:
                i = int(off + e["t"])
                if i < n:
                    seconds[role][i] = {**e, "t": i}
        for e in tl.get("events") or []:
            ev = {**e, "t": e["t"] + off}
            if "end" in e:
                ev["end"] = e["end"] + off
            events.append(ev)
        transcript += [
            {**s, "start": s["start"] + off, "end": s["end"] + off} for s in tl.get("transcript") or []
        ]
    silent = {"rms_db": -90.0, "peak_db": -90.0, "onsets": 0, "speech": False, "tags": {}}
    for entries in seconds.values():
        for i, e in enumerate(entries):
            if e is None:
                entries[i] = {"t": i, **silent}
    return {
        "version": first.get("version", TIMELINE_VERSION), "duration": round(duration, 3),
        "dead": not live, "dead_reason": "", "sources": first.get("sources") or {},
        "seconds": seconds, "events": sorted(events, key=lambda e: e["t"]), "transcript": transcript,
    }  # fmt: skip


def drop_raid_markers(events: list[dict], segs: list[eft.Segment]) -> list[dict]:
    """The recorder bookmarks every raid start/end (Manual -> "Bookmark"); those are not moments."""
    edges = [t for s in segs for t in (s.start, s.end)]
    return [
        e for e in events
        if not (e["kind"] == "hint" and e.get("phrase") == "Bookmark"
                and any(abs(e["t"] - t) <= RAID_EDGE_S for t in edges))
    ]  # fmt: skip


def _budget(seconds: float) -> int:
    return max(1, math.ceil(seconds / SECONDS_PER_CANDIDATE))


def pick(cands: list[Candidate], segs: list[eft.Segment], duration: float) -> list[Candidate]:
    """Hinted windows always; the rest best-first within each segment's budget (outside raids share one)."""
    hinted = [c for c in cands if any(r.startswith("hint:") for r in c.reasons)]
    rest = [c for c in cands if c not in hinted]
    if not segs:
        keep = rest[: max(max_candidates(), _budget(duration))]
    else:
        outside = duration - sum(s.end - s.start for s in segs)
        by_seg: dict[int, list[Candidate]] = {}
        for c in rest:
            seg = eft.segment_at(segs, c.peak_t)
            by_seg.setdefault(segs.index(seg) if seg else -1, []).append(c)
        keep = []
        for i, cs in by_seg.items():
            budget = _budget(outside) if i < 0 else _budget(segs[i].end - segs[i].start)
            keep += cs[:budget]
    return sorted(hinted + keep, key=lambda c: -c.score)


def label(c: Candidate, segs: list[eft.Segment]) -> Candidate:
    if not segs:
        return c
    seg = eft.segment_at(segs, c.peak_t) or eft.segment_at(segs, (c.start + c.end) / 2)
    return replace(c, reasons=[*c.reasons, f"map:{seg.label}" if seg else "outside raid"])


def to_file(c: Candidate, rec: Rec) -> Candidate:
    """A group candidate in `rec`'s own seconds, kept inside the file (at least MIN_LEN when it fits)."""
    start, end = max(0.0, c.start - rec.offset), min(rec.duration, c.end - rec.offset)
    if end - start < MIN_LEN:
        if start <= 0.0:
            end = min(rec.duration, start + MIN_LEN)
        else:
            start = max(0.0, end - MIN_LEN)
    peak = min(max(c.peak_t - rec.offset, start), end)
    return replace(c, start=start, end=end, peak_t=peak)


def _segments_of(recs: list[Rec], iopts: IngestOptions, log) -> list[eft.Segment]:
    t0 = recs[0].started
    if iopts.eft_logs is None:
        return []
    if t0 is None:
        log("  no wall-clock start for this recording; EFT raids skipped")
        return []
    duration = recs[-1].offset + recs[-1].duration
    until = t0 + timedelta(seconds=duration)
    raids = eft.read_raids(iopts.eft_logs, since=t0 - timedelta(hours=12), until=until)
    return eft.segments(raids, t0, duration)


def find_group_candidates(ctx: Context, recs: list[Rec], iopts: IngestOptions) -> None:
    todo = [r for r in recs if not r.skip and not ctx.store.stage_done(r.row["id"], "candidates")]
    if not todo:
        return
    t0 = datetime.now()
    segs = _segments_of(recs, iopts, ctx.log)
    gtl = group_timeline(recs)
    gtl["events"] = drop_raid_markers(gtl["events"], segs)
    cands = find_candidates(gtl, max_n=10**6, min_score=ctx.options.min_score)
    cands = [label(c, segs) for c in pick(cands, segs, gtl["duration"])]
    if segs:
        ctx.log("  raids: " + ", ".join(f"{s.label} {s.start:.0f}-{s.end:.0f}s" for s in segs))
    secs = (datetime.now() - t0).total_seconds() / len(todo)
    for r in todo:
        mine = [to_file(c, r) for c in cands if r.offset <= c.peak_t < r.offset + r.duration]
        local = [
            {**s.to_dict(), "start": max(0.0, s.start - r.offset), "end": min(r.duration, s.end - r.offset)}
            for s in segs if s.end > r.offset and s.start < r.offset + r.duration
        ]  # fmt: skip
        rid = r.row["id"]
        ctx.store.set_segments(rid, local)
        ctx.store.add_candidates(rid, [c.to_dict() for c in sorted(mine, key=lambda c: -c.score)])
        ctx.store.record_stage(rid, "candidates", secs)
        ctx.log(f"  {r.path.name}: {len(mine)} candidate(s)")


# --- driver -----------------------------------------------------------------------------------------------


def _prepare(ctx: Context, rec: Rec) -> None:
    """probe + wall-clock start + sampled dead check; sets rec.skip for files that need no work."""
    store, rid = ctx.store, rec.row["id"]
    status = rec.row["status"]
    if status in TERMINAL or (status == "failed" and not ctx.options.retry_failed):
        rec.res.skipped, rec.res.note, rec.skip = True, f"already {status}", True
    if ctx.options.retry_failed and not rec.skip:
        store.reset_failed_candidates(rid)
    if not store.stage_done(rid, "probe"):
        info = _timed(ctx, rid, "probe", lambda: media.probe(rec.path))
        store.update_replay(rid, duration=info.duration, kind="long")
    row = store.replay(rid)
    rec.duration = float(row["duration"] or 0.0)
    meta = hints.segra_metadata(rec.path)
    rec.started, source = started_at(rec.path, rec.duration, meta)
    if rec.started and row["started_at"] != rec.started.isoformat(sep=" "):
        store.update_replay(rid, started_at=rec.started.isoformat(sep=" "), kind="long")
        ctx.log(f"  starts {rec.started:%Y-%m-%d %H:%M:%S} (from {source})")
    rec.hints = hints.hints(rec.path, meta)
    if not store.stage_done(rid, "dead_check"):
        dead, reason = _timed(ctx, rid, "dead_check", lambda: media.is_dead_sampled(rec.path))
        store.update_replay(rid, dead=int(dead), dead_reason=reason if dead else "")
    row = store.replay(rid)
    if row["dead"]:
        if not rec.skip:
            store.update_replay(rid, status="dead")
            rec.res.status, rec.res.note = "dead", row["dead_reason"]
            ctx.log(f"  dead capture: {row['dead_reason']}")
        rec.skip = rec.dead = True
        return
    if not rec.skip:
        store.update_replay(rid, status="new", error="")


def ingest_group(ctx: Context, recs: list[Rec], iopts: IngestOptions) -> None:
    names = ", ".join(r.path.name for r in recs)
    ctx.log(f"{names}: ingesting ({len(recs)} file(s))")
    for r in recs:
        if not r.dead:
            r.timeline = _timeline(ctx, r, iopts)
    find_group_candidates(ctx, recs, iopts)
    for r in recs:
        if not r.skip and r.timeline is not None:
            judge_and_render(ctx, r.path, r.row["id"], r.res, r.timeline)


def ingest(ctx: Context, target: Path, iopts: IngestOptions | None = None) -> list[ReplayResult]:
    """Ingest `target` (a file, or every recording under a directory); one ReplayResult per file."""
    iopts = iopts or IngestOptions()
    files = list_recordings(target, ctx.settings.output_dir)
    stable = set(settle(files, iopts.stable_wait))
    results: list[ReplayResult] = []
    recs: list[Rec] = []
    for f in files:
        if f not in stable:
            results.append(ReplayResult(f, "unstable", note="still changing (or empty)", skipped=True))
            continue
        row = register(ctx, f)
        rec = Rec(f, row, ReplayResult(f, row["status"], row["id"]))
        results.append(rec.res)
        if row["status"] == "duplicate":
            rec.res.skipped, rec.res.note = True, "duplicate"
            continue
        try:
            _prepare(ctx, rec)
        except Exception as exc:  # a broken file must not stop the others
            _fail(ctx, [rec], exc)
            continue
        recs.append(rec)
    for group in group_splits(recs, iopts.split_gap_s):
        if ctx.should_stop():
            break
        offset = 0.0
        for r in group:
            r.offset, offset = offset, offset + r.duration
        if all(r.skip for r in group):
            continue
        try:
            ingest_group(ctx, group, iopts)
        except Exception as exc:
            _fail(ctx, [r for r in group if not r.skip], exc)
    return results


def inbox_uploads(inbox: Path) -> list[Path]:
    """The video of every finished upload, `<inbox>/<user>/<sha256>/<file>` with a meta.json beside it.

    `.staging/` (and any other dot directory) holds half-received uploads and is never read.
    """
    if not inbox.is_dir():
        return []
    out: list[Path] = []
    for user in sorted(inbox.iterdir()):
        if user.name.startswith(".") or not user.is_dir():
            continue
        for upload in sorted(user.iterdir()):
            meta = upload / "meta.json"
            if not upload.is_dir() or not meta.is_file():
                continue
            try:
                name = str(json.loads(meta.read_text()).get("file") or "")
            except (OSError, ValueError):
                continue
            video = upload / name
            if name and Path(name).name == name and video.suffix.lower() in VIDEO_EXTS and video.is_file():
                out.append(video)
    return out


def ingest_inbox(ctx: Context, iopts: IngestOptions | None = None) -> list[ReplayResult]:
    """Ingest the finished uploads that still need work, one at a time (checks `ctx.should_stop` between).

    Idempotent and cheap to repeat: a file the store already shows as finished is skipped by a stat lookup,
    without hashing or probing. Uploads are complete by construction (renamed into place), so there is no
    size-stability wait.
    """
    iopts = replace(iopts or IngestOptions(), stable_wait=0.0)
    results: list[ReplayResult] = []
    for video in inbox_uploads(inbox_root(ctx.settings)):
        if ctx.should_stop():
            break
        st = video.stat()
        row = ctx.store.replay_by_stat(video, st.st_size, st.st_mtime)
        if row and (
            row["status"] in TERMINAL or (row["status"] == "failed" and not ctx.options.retry_failed)
        ):
            continue
        results += ingest(ctx, video, iopts)
    return results


def _fail(ctx: Context, recs: list[Rec], exc: Exception) -> None:
    msg = f"{type(exc).__name__}: {exc}"[:500]
    for r in recs:
        ctx.store.update_replay(r.row["id"], status="failed", error=msg)
        r.res.status, r.res.note, r.skip = "failed", msg, True
    ctx.log(f"  FAILED {msg}")
