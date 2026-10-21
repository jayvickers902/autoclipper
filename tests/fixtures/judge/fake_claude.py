"""Fake `claude` CLI for tests (AUTOCLIPPER_FAKE_CLAUDE). Replays recorded stream-json.

    fake_claude.py STATE_DIR [claude args...]

STATE_DIR/plan.json: {"scenarios": [...], "ledger": "/path/ledger.sqlite"}. Call N plays scenario N (the
last one repeats). A scenario is a fixture name in this directory (`success` -> success.jsonl) or "hang"
(sleeps, with a grandchild sleeping too, so the caller must kill the process group). Placeholders
"__T+N__" / "__T-N__" become now +/- N epoch seconds. Exit code 1 when the result line is an error.
Each call is logged to STATE_DIR/call-N.json (argv, env keys, cwd, stdin stats, 'started' ledger rows).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLACEHOLDER = re.compile(r'"__T([+-]\d+)__"')


def main() -> int:
    state = Path(sys.argv[1])
    plan = json.loads((state / "plan.json").read_text())
    n = len(list(state.glob("call-*.json"))) + 1
    scenarios = plan["scenarios"]
    scenario = scenarios[min(n, len(scenarios)) - 1]

    stdin = sys.stdin.read()
    started = None
    if plan.get("ledger") and Path(plan["ledger"]).exists():
        con = sqlite3.connect(plan["ledger"])
        started = con.execute("SELECT COUNT(*) FROM calls WHERE status='started'").fetchone()[0]
        con.close()
    try:
        msg = json.loads(stdin)
        content = msg["message"]["content"]
        n_images = sum(1 for b in content if b.get("type") == "image")
    except (ValueError, KeyError, TypeError):
        n_images = -1
    log = {
        "n": n, "scenario": scenario, "argv": sys.argv[2:], "env": sorted(os.environ),
        "cwd": os.getcwd(), "cwd_entries": os.listdir("."), "stdin_lines": stdin.count("\n"),
        "n_images": n_images, "started_rows": started,
    }  # fmt: skip
    (state / f"call-{n}.json").write_text(json.dumps(log))

    if scenario == "hang":
        child = subprocess.Popen(["sleep", "300"])
        (state / f"hang-{n}.pid").write_text(str(child.pid))
        time.sleep(300)
        return 0

    now = time.time()
    text = (HERE / f"{scenario}.jsonl").read_text()
    text = PLACEHOLDER.sub(lambda m: str(int(now + int(m.group(1)))), text)
    is_error = False
    for line in text.splitlines():
        if line.startswith("{"):
            print(line, flush=True)
            d = json.loads(line)
            if d.get("type") == "result":
                is_error = bool(d.get("is_error"))
    return 1 if is_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
