"""Command-line entry point: `autoclipper <command>`."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from autoclipper import __version__


def _add_pipeline_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--judge", default=None, help="'fake' keeps every candidate offline (default: Claude)")
    p.add_argument("--no-whisper", action="store_true", help="skip speech transcription (faster, weaker)")
    p.add_argument(
        "--tags",
        action="store_true",
        default=os.environ.get("AUTOCLIPPER_TAGS") == "1",
        help="add AudioSet tags (gunshot, laughter, music) to the signals; or AUTOCLIPPER_TAGS=1",
    )
    p.add_argument("--stable-wait", type=float, default=2.0, metavar="S", help="seconds between size checks")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autoclipper", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")
    notify = sub.add_parser("notify", help="post a status line to the Discord test webhook")
    notify.add_argument("text")

    process = sub.add_parser(
        "process", help="run one replay through the pipeline (resumes unfinished stages)"
    )
    process.add_argument("file", type=Path)
    _add_pipeline_flags(process)

    backfill = sub.add_parser("backfill", help="process the top-level 'Replay *.mp4' files of a directory")
    backfill.add_argument("dir", type=Path)
    backfill.add_argument("--limit", type=int, default=None, help="process at most N replays")
    backfill.add_argument("--retry-failed", action="store_true")
    _add_pipeline_flags(backfill)

    watch = sub.add_parser("watch", help="poll the replay directory and process new replays")
    watch.add_argument("--interval", type=float, default=30.0, metavar="S")
    watch.add_argument(
        "--once", action="store_true", help="one pass (with its own size-stability wait), then exit"
    )
    watch.add_argument("--retry-failed", action="store_true")
    _add_pipeline_flags(watch)

    run = sub.add_parser("run", help="the hub: review page + watch loop in one process (SIGTERM stops)")
    run.add_argument("--interval", type=float, default=30.0, metavar="S")
    run.add_argument("--port", type=int, default=None, help="override AUTOCLIPPER_PORT (0 = any free port)")
    run.add_argument("--retry-failed", action="store_true")
    run.add_argument("--post", action="store_true", help="post newly kept clips to the Discord test webhook")
    _add_pipeline_flags(run)

    ingest = sub.add_parser(
        "ingest", help="long recordings (Segra sessions, OBS split files): chunked, EFT raids, chapter hints"
    )
    ingest.add_argument("target", type=Path, help="a recording, or a directory searched recursively")
    ingest.add_argument(
        "--eft-logs",
        type=Path,
        default=os.environ.get("AUTOCLIPPER_EFT_LOGS") or None,
        help="EFT Logs directory (log_*/...application_000.log) to label raids and maps; "
        "or AUTOCLIPPER_EFT_LOGS",
    )
    ingest.add_argument("--chunk", type=int, default=300, metavar="S", help="analysis chunk length (s)")
    ingest.add_argument("--overlap", type=int, default=10, metavar="S", help="context read around a chunk")
    _add_pipeline_flags(ingest)

    sub.add_parser(
        "upload-serve", help="the upload receiver for recorders (off unless AUTOCLIPPER_UPLOAD_PORT is set)"
    )

    retention = sub.add_parser(
        "retention", help="report-only plan: oldest own files to drop to get under AUTOCLIPPER_RETENTION_GB"
    )
    retention.add_argument("--json", action="store_true")

    report = sub.add_parser("report", help="counts, keeps, stage timings and judge calls")
    report.add_argument("--json", action="store_true")

    search = sub.add_parser("search", help="full-text search of judged clips (title, summary, map, speech)")
    search.add_argument("query", nargs="+", help="words to look for")
    search.add_argument("--limit", type=int, default=10, metavar="N")
    search.add_argument("--json", action="store_true")
    search.add_argument("--reindex", action="store_true", help="rebuild the search index first")

    align = sub.add_parser(
        "align", help="time offset between two friends' recordings (t_b - t_a), from their voice tracks"
    )
    align.add_argument("a", type=Path, help="recording A")
    align.add_argument("b", type=Path, help="recording B")
    align.add_argument("--a-layout", choices=("obs", "segra"), default="obs", help="track layout of A")
    align.add_argument("--b-layout", choices=("obs", "segra"), default="segra", help="track layout of B")
    align.add_argument("--max-offset", type=float, default=300.0, metavar="S", help="largest |offset| (s)")
    align.add_argument("--json", action="store_true")

    serve = sub.add_parser("serve", help="the local review page (default http://127.0.0.1:4180/)")
    serve.add_argument("--port", type=int, default=None, help="override AUTOCLIPPER_PORT (0 = any free port)")
    return parser


def _context(args: argparse.Namespace, retry_failed: bool):
    from autoclipper.config import Settings
    from autoclipper.judging import resolve_judge
    from autoclipper.pipeline import Context, Options
    from autoclipper.store import Store

    settings = Settings.from_env()
    return Context(
        settings=settings,
        store=Store(settings),
        judge=resolve_judge(settings, args.judge),
        options=Options(
            whisper=not getattr(args, "no_whisper", False),
            tags=getattr(args, "tags", False),
            retry_failed=retry_failed,
        ),
        log=lambda msg: print(msg, flush=True),
    )


def _summary(results: list) -> str:
    done = [r for r in results if not r.skipped]
    by: dict[str, int] = {}
    for r in done:
        by[r.status] = by.get(r.status, 0) + 1
    clips = sum(r.clips for r in done)
    kinds = ", ".join(f"{n} {k}" for k, n in sorted(by.items())) or "nothing to do"
    return f"{len(done)} replay(s) processed ({kinds}), {clips} clip(s)"


def _cmd_process(args: argparse.Namespace) -> int:
    from autoclipper.pipeline import process_replay
    from autoclipper.scan import settle

    ctx = _context(args, retry_failed=True)
    with ctx.store:
        path = args.file.resolve()
        if not path.is_file():
            print(f"autoclipper: {path} is not a file", file=sys.stderr)
            return 2
        if path.is_relative_to(ctx.settings.output_dir.resolve()):
            print("autoclipper: refusing to process a file in the output directory", file=sys.stderr)
            return 2
        if not settle([path], args.stable_wait):
            print(f"autoclipper: {path.name} is still changing (or empty); try again later", file=sys.stderr)
            return 3
        res = process_replay(ctx, path)
    print(f"{path.name}: {res.status}" + (f" ({res.note})" if res.note else ""))
    return 1 if res.status == "failed" else 0


def _cmd_backfill(args: argparse.Namespace) -> int:
    from autoclipper.pipeline import process_pass

    ctx = _context(args, retry_failed=args.retry_failed)
    with ctx.store:
        results = process_pass(ctx, args.dir, limit=args.limit, stable_wait=args.stable_wait)
    print(_summary(results))
    return 1 if any(r.status == "failed" for r in results) else 0


def _cmd_watch(args: argparse.Namespace) -> int:
    from autoclipper.hub import replay_pass
    from autoclipper.scan import StabilityTracker

    ctx = _context(args, retry_failed=args.retry_failed)
    tracker = None if args.once else StabilityTracker()
    with ctx.store:
        try:
            while True:
                results = replay_pass(ctx, tracker, args.stable_wait)
                if results or args.once:
                    print(_summary(results), flush=True)
                if args.once:
                    return 1 if any(r.status == "failed" for r in results) else 0
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


def _cmd_run(args: argparse.Namespace) -> int:
    import dataclasses
    import signal
    import threading

    from autoclipper.gpu_gate import GpuGate
    from autoclipper.hub import run_hub

    ctx = _context(args, retry_failed=args.retry_failed)
    if args.port is not None:
        ctx.settings = dataclasses.replace(ctx.settings, port=args.port)
    ctx.gpu_gate = GpuGate(log=ctx.log)
    stop = threading.Event()

    def on_signal(signum: int, _frame: object) -> None:
        if stop.is_set():  # a second signal abandons the current stage
            raise KeyboardInterrupt
        ctx.log(f"autoclipper: signal {signum}, finishing the current replay then stopping")
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    with ctx.store:
        return run_hub(
            ctx, stop=stop, interval=args.interval, stable_wait=args.stable_wait, post=args.post
        )


def _cmd_ingest(args: argparse.Namespace) -> int:
    from autoclipper.ingest import IngestOptions, ingest

    if args.chunk < 30 or not 0 <= args.overlap < args.chunk:
        print("autoclipper: --chunk must be >= 30 and --overlap in [0, chunk)", file=sys.stderr)
        return 2
    ctx = _context(args, retry_failed=True)
    with ctx.store:
        target = args.target.resolve()
        if not target.exists():
            print(f"autoclipper: {target} does not exist", file=sys.stderr)
            return 2
        if target.is_relative_to(ctx.settings.output_dir.resolve()):
            print("autoclipper: refusing to ingest from the output directory", file=sys.stderr)
            return 2
        opts = IngestOptions(
            eft_logs=args.eft_logs, chunk_s=args.chunk, overlap_s=args.overlap, stable_wait=args.stable_wait
        )
        results = ingest(ctx, target, opts)
    for r in results:
        print(f"{r.path.name}: {r.status}" + (f" ({r.note})" if r.note else ""))
    print(_summary(results))
    return 1 if any(r.status == "failed" for r in results) else 0


def _cmd_upload_serve(args: argparse.Namespace) -> int:
    from autoclipper.config import Settings
    from autoclipper.uploads import serve

    try:
        serve(Settings.from_env(), log=lambda msg: print(msg, flush=True))
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_retention(args: argparse.Namespace) -> int:
    from autoclipper.config import Settings
    from autoclipper.retention import format_plan, plan
    from autoclipper.store import Store

    settings = Settings.from_env()
    with Store(settings) as store:
        result = plan(settings, store)
    print(json.dumps(result.to_dict(), indent=1) if args.json else format_plan(result))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from autoclipper.config import Settings
    from autoclipper.report import build_report, format_report
    from autoclipper.store import Store

    with Store(Settings.from_env()) as store:
        data = build_report(store)
    print(json.dumps(data, indent=1) if args.json else format_report(data))
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    from autoclipper.config import Settings
    from autoclipper.review import fmt_time
    from autoclipper.search import MARK_CLOSE, MARK_OPEN
    from autoclipper.store import Store

    if args.limit < 1:
        print("autoclipper: --limit must be at least 1", file=sys.stderr)
        return 2
    with Store(Settings.from_env()) as store:
        if args.reindex:
            print(f"reindexed {store.rebuild_search()} judged candidate(s)", file=sys.stderr)
        hits = store.search(" ".join(args.query), args.limit)
    for h in hits:
        h["snippet"] = h["snippet"].replace(MARK_OPEN, "[").replace(MARK_CLOSE, "]")
    if args.json:
        print(json.dumps(hits, indent=1))
        return 0
    if not hits:
        print("no matches")
    for i, h in enumerate(hits, 1):
        when = f"{fmt_time(h['start'])}-{fmt_time(h['end'])}"
        print(f"{i}. {h['score']:.2f}  {Path(h['replay_path']).name}  {when}  {h['title']}")
        print(f"   {h['snippet']}")
        print(f"   clip: {h['clip_path'] or '-'}")
    return 0


def _cmd_align(args: argparse.Namespace) -> int:
    from autoclipper.align import OBS_TRACKS, SEGRA_TRACKS, align
    from autoclipper.media import MediaError

    layouts = {"obs": OBS_TRACKS, "segra": SEGRA_TRACKS}
    la, lb = layouts[args.a_layout], layouts[args.b_layout]
    for p in (args.a, args.b):
        if not p.is_file():
            print(f"autoclipper: {p} is not a file", file=sys.stderr)
            return 2
    try:
        r = align(
            args.a, args.b, a_mic_track=la["mic"], b_voice_track=lb["voice"], b_mic_track=lb["mic"],
            a_voice_track=la["voice"], max_offset_s=args.max_offset,
        )  # fmt: skip
    except MediaError as e:
        print(f"autoclipper: {e}", file=sys.stderr)
        return 1
    latency = r.details.get("latency_s")
    if args.json:
        print(json.dumps({"offset_s": r.offset_s, "confidence": r.confidence, "method": r.method,
                          "latency_s": latency, "details": r.details}, indent=1))  # fmt: skip
        return 0
    print(f"offset (t_b - t_a): {r.offset_s:+.3f} s")
    print(f"confidence: {r.confidence:.2f}  ({r.method})")
    print(f"discord latency: {f'{latency:.3f} s' if latency is not None else 'unknown'}")
    if r.details.get("note"):
        print(f"note: {r.details['note']}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import dataclasses

    from autoclipper.config import Settings
    from autoclipper.review import serve

    settings = Settings.from_env()
    if args.port is not None:
        settings = dataclasses.replace(settings, port=args.port)
    serve(settings, log=lambda msg: print(msg, flush=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
    elif args.command == "notify":
        from autoclipper.discord import post_text

        result = post_text(args.text)
        print(f"notify {result}")
        return 0 if result.ok else 1
    elif args.command == "process":
        return _cmd_process(args)
    elif args.command == "backfill":
        return _cmd_backfill(args)
    elif args.command == "watch":
        return _cmd_watch(args)
    elif args.command == "run":
        return _cmd_run(args)
    elif args.command == "ingest":
        return _cmd_ingest(args)
    elif args.command == "upload-serve":
        return _cmd_upload_serve(args)
    elif args.command == "retention":
        return _cmd_retention(args)
    elif args.command == "report":
        return _cmd_report(args)
    elif args.command == "search":
        return _cmd_search(args)
    elif args.command == "align":
        return _cmd_align(args)
    elif args.command == "serve":
        return _cmd_serve(args)
    return 0
