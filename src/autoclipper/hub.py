"""The hub service: review page (thread) + watch loop (main thread) in one process, stopped by an Event.

SIGTERM sets the event (see `cli._cmd_run`). The pass in progress finishes its current replay, the rest are
left for the next start (every stage is resumable), then the HTTP server is shut down and closed.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import media
from .config import bind_socket
from .discord import DiscordPoster
from .ingest import IngestOptions, ingest_inbox
from .pipeline import Context, ReplayResult, _clamp, process_pass
from .review import make_server
from .scan import StabilityTracker


def make_poster(ctx: Context) -> DiscordPoster:
    """A poster whose re-encodes go through the GPU gate. Caps and the webhook check live in DiscordPoster."""
    gate = ctx.gpu_gate

    def encoder(*args: Any, **kw: Any) -> media.EncodeResult:
        return media.share_encode(*args, encoder=gate.encoder() if gate else "auto", **kw)

    return DiscordPoster(ctx.settings, encoder=encoder)


def post_new_clips(ctx: Context, results: list[ReplayResult], poster: Any) -> int:
    """Post the unposted kept clips of the replays this pass processed; returns how many were posted.

    Stops at the first refusal (cap reached or no webhook) so a capped hub does not retry every clip.
    """
    posted = 0
    for res in results:
        if res.skipped or res.replay_id is None:
            continue
        for clip in ctx.store.unposted_clips(res.replay_id):
            cand = clip["candidate"]
            verdict = cand["verdict"] or {}
            start, end, _ = _clamp(verdict, cand)
            out = poster.post_clip(
                clip["share_path"], str(verdict.get("title") or "clip"), str(verdict.get("game") or "other"),
                end - start, str(verdict.get("summary") or ""),
            )  # fmt: skip
            ctx.log(f"  post {res.path.name} clip {cand['idx'] + 1}: {out}")
            if out.status in ("refused", "skipped"):
                return posted
            if out.ok:
                ctx.store.save_clip(cand["id"], posted=1, posted_at=time.time())
                posted += 1
    return posted


def ingest_uploads(ctx: Context) -> list[ReplayResult]:
    """Ingest finished inbox uploads (P3.4/P4.1). EFT logs come from AUTOCLIPPER_EFT_LOGS like `ingest`."""
    logs = os.environ.get("AUTOCLIPPER_EFT_LOGS")
    return ingest_inbox(ctx, IngestOptions(eft_logs=Path(logs) if logs else None))


def replay_pass(ctx: Context, tracker: StabilityTracker | None, stable_wait: float) -> list[ReplayResult]:
    """One loop iteration: new replays, then finished uploads. An inbox failure keeps the replay results."""
    results = process_pass(ctx, tracker=tracker, stable_wait=stable_wait)
    if ctx.should_stop():
        return results
    try:
        results += ingest_uploads(ctx)
    except Exception as exc:
        ctx.log(f"inbox ingest failed: {type(exc).__name__}: {exc}")
    return results


def pass_line(results: list[ReplayResult], posted: int | None) -> str:
    done = [r for r in results if not r.skipped]
    by: dict[str, int] = {}
    for r in done:
        by[r.status] = by.get(r.status, 0) + 1
    kinds = ", ".join(f"{n} {k}" for k, n in sorted(by.items())) or "idle"
    line = f"pass: {len(done)} replay(s) ({kinds}), {sum(r.clips for r in done)} clip(s)"
    return line if posted is None else f"{line}, {posted} posted"


def run_hub(
    ctx: Context,
    *,
    stop: threading.Event,
    interval: float = 30.0,
    stable_wait: float = 2.0,
    post: bool = False,
    poster: Any = None,
    on_ready: Callable[[int], None] | None = None,
) -> int:
    """Serve the review page and run the watch loop until `stop` is set. Returns the exit code."""
    ctx.should_stop = stop.is_set
    sock = bind_socket(ctx.settings.host, ctx.settings.port)
    port = sock.getsockname()[1]
    server = make_server(ctx.settings, sock)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    host = ctx.settings.host
    ctx.log(f"autoclipper review on http://{host}:{port}/ (http://autoclipper.localhost/ via Caddy)")
    if on_ready:
        on_ready(port)
    if post and poster is None:
        poster = make_poster(ctx)
    tracker = StabilityTracker()
    try:
        while not stop.is_set():
            try:
                results = replay_pass(ctx, tracker, stable_wait)
                posted = post_new_clips(ctx, results, poster) if post else None
                ctx.log(pass_line(results, posted))
            except Exception as exc:  # one bad pass (unreadable dir, store error) must not end the service
                ctx.log(f"pass failed: {type(exc).__name__}: {exc}")
            stop.wait(interval)
    except KeyboardInterrupt:
        stop.set()
    finally:
        ctx.log("autoclipper: stopping")
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return 0
