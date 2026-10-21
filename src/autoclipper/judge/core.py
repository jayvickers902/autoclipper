"""Judge one candidate: Claude Haiku (guarded, retry once), then the ollama fallback; Sonnet polish."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from .claude_cli import CallResult, run_claude
from .frames import extract_frames, frame_times, image_block
from .guard import Guard, GuardRefused
from .ollama import ollama_judge
from .prompt import (
    SYSTEM_JUDGE,
    SYSTEM_POLISH,
    SYSTEM_REJUDGE,
    candidate_dict,
    context_text,
    prior_verdict_text,
)
from .schema import SchemaError, Verdict, is_borderline, validate, verdict_schema

ATTEMPTS = 2  # a failed call is retried once


@dataclass
class JudgeResult:
    verdict: Verdict
    ledger_rows: list[int] = field(default_factory=list)  # Claude calls (and the fallback row) made
    cost_usd: float = 0.0  # notional API price of the Claude calls; nothing is billed
    input_tokens: int = 0  # including cache reads and writes
    output_tokens: int = 0
    attempts: list[str] = field(default_factory=list)  # one line per call or refusal, for logs

    @property
    def queued(self) -> bool:
        """True when Claude should judge this candidate again once the guard allows it."""
        return self.verdict.queued

    def to_dict(self) -> dict:
        return {**dataclasses.asdict(self), "verdict": self.verdict.to_dict(), "queued": self.queued}


@dataclass
class _Ctx:
    replay: Path
    cand: dict
    s: Settings
    guard: Guard
    cid: str
    raw_dir: Path | None
    start: float = 0.0
    end: float = 0.0
    times: list[float] = field(default_factory=list)
    text: str = ""
    _frames: list[tuple[float, str]] | None = None

    @property
    def schema(self) -> dict:
        return verdict_schema(self.start, self.end)

    def frames(self) -> list[tuple[float, str]]:
        if self._frames is None:
            self._frames = extract_frames(self.replay, self.times)
        return self._frames

    def content(self, extra: str = "") -> list[dict]:
        blocks: list[dict] = []
        for t, b64 in self.frames():
            blocks += [{"type": "text", "text": f"Frame at {t:.1f} s:"}, image_block(b64)]
        return blocks + [{"type": "text", "text": self.text + (f"\n\n{extra}" if extra else "")}]


def _ctx(replay_path, candidate, timeline, settings, candidate_id, guard, raw_dir) -> _Ctx:
    s = settings or Settings.from_env()
    cand = candidate_dict(candidate)
    start, end = float(cand["start"]), float(cand["end"])
    c = _Ctx(
        replay=Path(replay_path), cand=cand, s=s, guard=guard or Guard(s),
        cid=candidate_id or f"{Path(replay_path).name}@{start:.2f}-{end:.2f}",
        raw_dir=Path(raw_dir) if raw_dir else None, start=start, end=end, times=frame_times(start, end),
    )  # fmt: skip
    c.text = context_text(replay_path, cand, timeline, c.times)
    return c


def _tally(out: JudgeResult, res: CallResult) -> None:
    u = res.usage or {}
    out.input_tokens += sum(
        int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    out.output_tokens += int(u.get("output_tokens") or 0)
    out.cost_usd = round(out.cost_usd + (res.cost_usd or 0.0), 8)


def _claude(c: _Ctx, out: JudgeResult, role: str, model: str, system: str, extra: str = "") -> dict | None:
    """Up to ATTEMPTS guarded calls; the validated answer, or None (reasons go to out.attempts)."""
    content = c.content(extra)
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with c.guard.slot(role, model, c.cid) as row:
                res = run_claude(c.s.claude_bin, model, system, c.schema, content, c.s.judge_timeout_s)
                data = None
                if res.ok:
                    try:
                        data = validate(res.structured, c.start, c.end)
                    except SchemaError as e:
                        res.ok, res.status, res.error = False, "invalid", f"schema: {e}"
                c.guard.record(row, res)
        except GuardRefused as e:
            out.attempts.append(f"{model} refused: {e.reason}")
            return None
        out.ledger_rows.append(row)
        _tally(out, res)
        if c.raw_dir:
            c.raw_dir.mkdir(parents=True, exist_ok=True)
            (c.raw_dir / f"call-{row}.jsonl").write_text(res.stdout)
        out.attempts.append(
            f"{model} #{attempt}: {res.status}" + (f" ({res.error[:160]})" if res.error else "")
        )
        if data is not None:
            return data
    return None


def _fallback(c: _Ctx, out: JudgeResult) -> JudgeResult:
    queued = c.s.judge == "claude-cli"
    images = [b64 for _, b64 in c.frames()] if c.s.ollama_vision else []
    o = ollama_judge(c.s, SYSTEM_JUDGE, c.text, images, c.schema)
    data, err = None, o.error
    if o.ok:
        try:
            data = validate(o.data, c.start, c.end)
        except SchemaError as e:
            err = f"ollama {o.model} schema: {e}"
    status = "ok" if data else "unavailable" if o.unavailable else "error"
    c.guard.record_fallback(o.model, status, o.duration_s, err, c.cid)
    out.attempts.append(f"ollama {o.model}: {status}" + (f" ({err[:160]})" if err else ""))
    if data:
        out.verdict = Verdict(**data, source="ollama", model=o.model, queued=queued, candidate_id=c.cid)
    else:
        out.verdict = Verdict(
            start=c.start, end=c.end, thumb_t=float(c.cand.get("peak_t") or c.start), source="none",
            queued=True, error="; ".join(out.attempts)[-500:], candidate_id=c.cid,
        )  # fmt: skip
    return out


def judge_candidate(
    replay_path: Path | str,
    candidate: object,
    timeline: dict,
    settings: Settings | None = None,
    *,
    candidate_id: str | None = None,
    guard: Guard | None = None,
    rejudge_borderline: bool = False,
    raw_dir: Path | str | None = None,
) -> JudgeResult:
    """Judge one candidate window (a candidates.Candidate or its dict) of `replay_path`.

    Claude Haiku first (when settings.judge == "claude-cli" and the guard allows it), retried once on
    failure; then the local ollama judge. A fallback verdict has queued=True so Claude re-judges it later;
    source "none" means nothing could judge it. With rejudge_borderline, a borderline Haiku verdict goes to
    polish(mode="rejudge"). Raises MediaError if no frame decodes, LiveCallInTests on a real call in tests.
    """
    c = _ctx(replay_path, candidate, timeline, settings, candidate_id, guard, raw_dir)
    out = JudgeResult(verdict=Verdict(candidate_id=c.cid))
    if c.s.judge == "claude-cli":
        reason = c.guard.check("judge", c.s.judge_model)
        if reason:
            out.attempts.append(f"{c.s.judge_model} refused: {reason}")
        else:
            data = _claude(c, out, "judge", c.s.judge_model, SYSTEM_JUDGE)
            if data:
                out.verdict = Verdict(
                    **data, source="claude-haiku", model=c.s.judge_model, candidate_id=c.cid
                )
                if rejudge_borderline and is_borderline(out.verdict):
                    _polish(c, out, "rejudge")
                return out
    return _fallback(c, out)


def _polish(c: _Ctx, out: JudgeResult, mode: str) -> JudgeResult:
    prev = out.verdict
    system = SYSTEM_REJUDGE if mode == "rejudge" else SYSTEM_POLISH
    data = _claude(c, out, "polish", c.s.polish_model, system, prior_verdict_text(prev.to_dict(), prev.model))
    if data:
        out.verdict = Verdict(**data, source="claude-sonnet", model=c.s.polish_model, candidate_id=c.cid)
    else:  # keep the verdict as it was; `error` means "no usable verdict", so the reason stays in attempts
        out.attempts.append(f"{mode} failed; kept the {prev.source} verdict")
    return out


def polish(
    replay_path: Path | str,
    candidate: object,
    timeline: dict,
    verdict: Verdict,
    settings: Settings | None = None,
    *,
    mode: str = "polish",
    guard: Guard | None = None,
    raw_dir: Path | str | None = None,
) -> JudgeResult:
    """Sonnet pass over an existing verdict: mode "polish" (a clip being posted) or "rejudge" (borderline).

    Same guard, retry and ledger as judging (role "polish", daily cap). On failure or refusal the input
    verdict comes back unchanged (the reason is in `attempts`); there is no ollama fallback for polishing.
    """
    if mode not in ("polish", "rejudge"):
        raise ValueError(f"mode must be 'polish' or 'rejudge', not {mode!r}")
    c = _ctx(replay_path, candidate, timeline, settings, verdict.candidate_id or None, guard, raw_dir)
    return _polish(c, JudgeResult(verdict=verdict), mode)


def dumps(result: JudgeResult) -> str:
    return json.dumps(result.to_dict(), indent=2)
