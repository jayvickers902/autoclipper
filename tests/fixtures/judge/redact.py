"""Redact a captured `claude -p` stream-json file for use as a test fixture.

    python tests/fixtures/judge/redact.py RAW.jsonl OUT.jsonl

Replaces session/message/tool/request ids, thinking signatures and temp paths, and empties the init
line's tool/plugin/skill lists. Everything else (usage, rate_limit_event, result) is kept verbatim.
"""

from __future__ import annotations

import json
import re
import sys

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
IDS = re.compile(r"\b(msg|toolu|req)_[0-9A-Za-z]+")
LISTS = ("tools", "mcp_servers", "slash_commands", "terminal_slash_commands", "agents", "skills", "plugins",
         "capabilities", "additional_directories")  # fmt: skip


def _scrub(o: object) -> object:
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            if k == "signature":
                out[k] = "REDACTED"
            elif k in ("cwd", "original_cwd"):
                out[k] = "/tmp/ac-judge-cwd-redacted"
            elif k == "messaging_socket_path":
                continue
            elif k in LISTS and isinstance(v, list):
                out[k] = []
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(o, list):
        return [_scrub(v) for v in o]
    if isinstance(o, str):
        return IDS.sub(
            lambda m: f"{m.group(1)}_REDACTED", UUID.sub("00000000-0000-0000-0000-000000000000", o)
        )
    return o


def redact_line(line: str) -> str:
    line = line.strip()
    if not line.startswith("{"):
        return line
    return json.dumps(_scrub(json.loads(line)), ensure_ascii=False)


if __name__ == "__main__":
    src, dst = sys.argv[1:3]
    with open(src) as f, open(dst, "w") as g:
        for line in f:
            if line.strip():
                g.write(redact_line(line) + "\n")
