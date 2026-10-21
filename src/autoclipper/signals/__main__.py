"""Manual check: uv run python -m autoclipper.signals <file> [out.json]"""

from __future__ import annotations

import json
import sys
import time

from .analyze import analyze


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    t0 = time.perf_counter()
    result = analyze(argv[0], argv[1] if len(argv) > 1 else None)
    wall = time.perf_counter() - t0
    if len(argv) > 1:
        kinds: dict[str, int] = {}
        for e in result["events"]:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        print(f"{argv[0]}: {result['duration']}s, dead={result['dead']}, events={kinds}, wall {wall:.1f}s")
    else:
        json.dump(result, sys.stdout, indent=1)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
