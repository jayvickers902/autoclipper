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

    report = sub.add_parser("report", help="counts, keeps, stage timings and judge calls")
    report.add_argument("--json", action="store_true")

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
    from autoclipper.pipeline import process_pass
    from autoclipper.scan import StabilityTracker

    ctx = _context(args, retry_failed=args.retry_failed)
    tracker = None if args.once else StabilityTracker()
    with ctx.store:
        try:
            while True:
                results = process_pass(ctx, tracker=tracker, stable_wait=args.stable_wait)
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


def _cmd_report(args: argparse.Namespace) -> int:
    from autoclipper.config import Settings
    from autoclipper.report import build_report, format_report
    from autoclipper.store import Store

    with Store(Settings.from_env()) as store:
        data = build_report(store)
    print(json.dumps(data, indent=1) if args.json else format_report(data))
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
    elif args.command == "report":
        return _cmd_report(args)
    elif args.command == "serve":
        return _cmd_serve(args)
    return 0
