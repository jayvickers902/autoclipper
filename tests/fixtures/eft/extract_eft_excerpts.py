"""Rebuild the redacted EFT log excerpts in this directory from Jay's real logs.

READ-ONLY on the source tree. Usage: uv run python tests/fixtures/eft/extract_eft_excerpts.py
The excerpts are committed; this script only documents (and reproduces) how they were made.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path("/mnt/c/Battlestate Games/Escape from Tarkov/Logs")
DEST = Path(__file__).resolve().parent
# (log dir name, kind). Same directory and file names are kept, so "latest log_* dir" logic can be tested.
SOURCES = ["log_2026.10.12_20-22-21_1.2.0.0.47965", "log_2026.10.12_21-32-05_1.2.0.0.47965"]

TS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\|")
HEX24 = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{24}(?![0-9a-fA-F])")


def redact(line: str) -> str:
    line = HEX24.sub("REDACTED_ID", line)
    line = re.sub(r"(ProfileId:|Profileid: )REDACTED_ID", r"\1REDACTED_PROFILE_ID", line)
    line = re.sub(r"(AccountId:)\d+", r"\g<1>1234567", line)
    line = re.sub(r"(Matching with group id: )\d+", r"\g<1>1234567", line)
    line = re.sub(r"(Ip: )\d{1,3}(?:\.\d{1,3}){3}", r"\g<1>203.0.113.10", line)
    line = re.sub(r"(shortId: )\w+", r"\g<1>ABC123", line)
    line = re.sub(r"(Sid: [\w-]+?_)REDACTED_ID(_)", r"\1REDACTED_RAID_ID\2", line)
    return line


def keep(line: str, seen: dict[str, int]) -> bool:
    if not TS.match(line):
        return False  # continuation lines (settings JSON blocks)
    if "|Game settings:" in line or "|Control settings:" in line:
        return False
    for key, limit in (("GC::CollectAsync iteration", 2), ("Reason:Stuck", 2)):
        if key in line:
            seen[key] = seen.get(key, 0) + 1
            return seen[key] <= limit
    return True


def main() -> None:
    for name in SOURCES:
        src = SRC / name / f"{name[4:]} application_000.log"
        out_dir = DEST / name
        out_dir.mkdir(exist_ok=True)
        seen: dict[str, int] = {}
        lines = [
            redact(ln)
            for ln in src.read_text(encoding="utf-8", errors="replace").splitlines()
            if keep(ln, seen)
        ]
        (out_dir / src.name).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        print(out_dir.name, len(lines), "lines")


if __name__ == "__main__":
    main()
