"""`python -m autoclipper.judge status | clear-pause | new-run | one <replay> [--rank N] [--raw-dir DIR]`."""

from __future__ import annotations

import argparse
import json
import sys

from ..config import Settings
from .core import dumps, judge_candidate
from .guard import Guard


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m autoclipper.judge")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="guard state: pause, caps, tokens, utilization")
    sub.add_parser("clear-pause", help="lift a pause (e.g. after re-running `claude` /login)")
    sub.add_parser("new-run", help="start a new run epoch: the Haiku/Sonnet run caps count from now")
    one = sub.add_parser("one", help="analyze a replay and judge one candidate (makes a real Claude call)")
    one.add_argument("replay")
    one.add_argument("--rank", type=int, default=0, help="which candidate, 0 = best")
    one.add_argument("--timeline", help="reuse a timeline JSON instead of analyzing")
    one.add_argument("--no-whisper", action="store_true")
    one.add_argument("--raw-dir", help="save each call's raw stream-json here")
    a = p.parse_args(argv)

    s = Settings.from_env()
    guard = Guard(s)
    if a.cmd == "status":
        print(json.dumps(guard.status(), indent=2))
        return 0
    if a.cmd == "clear-pause":
        guard.clear_pause()
        print("pause cleared")
        return 0
    if a.cmd == "new-run":
        before, after = guard.new_run()
        caps = {"haiku": s.run_max_haiku, "sonnet": s.run_max_sonnet}
        for label, counts in (("before", before), ("after", after)):
            print(f"{label}: " + ", ".join(f"{f} {n}/{caps[f]}" for f, n in counts.items()))
        return 0

    from ..candidates import find_candidates
    from ..signals.analyze import analyze

    if a.timeline:
        with open(a.timeline) as f:
            timeline = json.load(f)
    else:
        timeline = analyze(a.replay, whisper=not a.no_whisper)
    cands = find_candidates(timeline)
    if len(cands) <= a.rank:
        print(f"only {len(cands)} candidates", file=sys.stderr)
        return 1
    print(dumps(judge_candidate(a.replay, cands[a.rank], timeline, s, guard=guard, raw_dir=a.raw_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
