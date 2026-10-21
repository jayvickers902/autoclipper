"""Clip judge: Claude through the `claude` CLI on Jay's subscription, guarded; ollama as the fallback.

Public API:
    judge_candidate(replay_path, candidate, timeline, settings=None, *, ...) -> JudgeResult
    polish(replay_path, candidate, timeline, verdict, settings=None, *, mode="polish") -> JudgeResult
        (mode "polish" for a clip being posted, "rejudge" for a borderline verdict)
    Guard(settings).status() -> dict      # pause, caps, tokens, last utilization (for `report`)
    Guard(settings).clear_pause()         # after Jay fixes a sign-in failure
"""

from .claude_cli import LiveCallInTests
from .core import JudgeResult, judge_candidate, polish
from .guard import Guard, GuardRefused
from .schema import Verdict, is_borderline

__all__ = [
    "Guard", "GuardRefused", "JudgeResult", "LiveCallInTests", "Verdict", "is_borderline",
    "judge_candidate", "polish",
]  # fmt: skip
