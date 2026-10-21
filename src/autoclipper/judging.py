"""The pipeline's view of the judge: a small Protocol, an adaptor onto `autoclipper.judge`, and a fake.

`autoclipper.judge` is imported lazily, at call time, and only `judge_candidate(replay_path, candidate,
timeline, settings)` and `Guard(settings).status()` are touched. If their signatures or result shapes change,
fix `ClaudeJudge.__call__` / `ClaudeJudge.paused` / `to_outcome` here and nothing else.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .candidates import Candidate
from .config import Settings


@dataclass(frozen=True)
class JudgeOutcome:
    verdict: dict  # keep, score, type, title, summary, start, end, thumb_t, game (+ whatever else)
    queued: bool = False  # Claude paused/unavailable: judge this candidate again after the reset
    error: str = ""  # set when no usable verdict came back (and not queued)
    source: str = ""  # claude-haiku / claude-sonnet / ollama / fake
    model: str = ""
    cost_usd: float = 0.0

    @property
    def keep(self) -> bool:
        return bool(self.verdict.get("keep"))


class Judge(Protocol):
    """A judge takes (replay path, candidate, timeline) and returns a JudgeOutcome.

    It may also have `paused() -> str | None`: a reason string while Claude is unavailable (the pipeline then
    queues candidates without calling it), else None.
    """

    def __call__(self, replay: Path, candidate: Candidate, timeline: dict) -> JudgeOutcome: ...


def _get(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def to_outcome(res: Any) -> JudgeOutcome:
    """Duck-typed conversion of a judge result: `.verdict` (Verdict or dict) or the verdict itself."""
    verdict = _get(res, "verdict", res)
    vd = dict(verdict) if isinstance(verdict, dict) else verdict.to_dict()
    queued = bool(_get(res, "queued", False) or vd.get("queued"))
    cost = _get(res, "cost_usd", None)
    if cost is None:
        cost = _get(res, "cost", 0.0)
    return JudgeOutcome(
        verdict=vd,
        queued=queued,
        error=str(_get(res, "error", "") or vd.get("error") or ""),
        source=str(_get(res, "source", "") or vd.get("source") or ""),
        model=str(_get(res, "model", "") or vd.get("model") or ""),
        cost_usd=float(cost or 0.0),
    )


class ClaudeJudge:
    """Default judge: autoclipper.judge.judge_candidate, resolved when first called."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def paused(self) -> str | None:
        from autoclipper.judge.guard import Guard

        st = Guard(self.settings).status()
        return str(st.get("reason") or "claude paused") if st.get("paused") else None

    def __call__(self, replay: Path, candidate: Candidate, timeline: dict) -> JudgeOutcome:
        import autoclipper.judge as judge_pkg

        out = to_outcome(judge_pkg.judge_candidate(replay, candidate, timeline, self.settings))
        return apply_keep_threshold(out, self.settings.keep_min_score)


def apply_keep_threshold(out: JudgeOutcome, min_score: int) -> JudgeOutcome:
    """Decide keep from the score (the model's own flag stays in verdict["model_keep"]); 0 leaves it alone."""
    v = out.verdict
    if min_score <= 0 or not v or not isinstance(v.get("score"), (int, float)):
        return out
    v = {**v, "model_keep": v.get("keep"), "keep": v["score"] >= min_score}
    return dataclasses.replace(out, verdict=v)


class FakeJudge:
    """Offline stand-in (`--judge fake`): keeps every candidate as-is. Never touches Claude or ollama."""

    paused = None

    def __call__(self, replay: Path, candidate: Candidate, timeline: dict) -> JudgeOutcome:
        thumb = min(max(candidate.peak_t, candidate.start), candidate.end)
        verdict = {
            "keep": True, "score": 7, "type": "firefight", "title": f"Fake clip at {candidate.start:.0f}s",
            "summary": "fake judge: kept unconditionally", "start": candidate.start, "end": candidate.end,
            "thumb_t": thumb, "game": "other",
        }  # fmt: skip
        return JudgeOutcome(verdict=verdict, source="fake", model="fake")


def resolve_judge(settings: Settings, name: str | None = None) -> Judge:
    """`name` (from --judge) or $AUTOCLIPPER_PIPELINE_JUDGE: "fake" gives FakeJudge, anything else Claude."""
    name = name or os.environ.get("AUTOCLIPPER_PIPELINE_JUDGE") or ""
    return FakeJudge() if name == "fake" else ClaudeJudge(settings)
