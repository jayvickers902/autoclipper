"""Judge comparison: metrics, stratified sample, budgets and the report. No real Claude or ollama calls."""

from __future__ import annotations

import argparse
import dataclasses
import json

import pytest

import autoclipper.judge.compare as cmp
from autoclipper.config import Settings
from autoclipper.judge.ollama import OllamaResult
from autoclipper.store import Store


def V(keep, score, game="tarkov", type_="kill"):
    return {"keep": keep, "score": score, "game": game, "type": type_}


def test_agreement_confusion_kappa_and_scores():
    ref = {1: V(True, 8), 2: V(True, 7), 3: V(False, 2), 4: V(False, 3, "cs2")}
    judge = {1: V(True, 7), 2: V(False, 5), 3: V(False, 2), 4: V(True, 6, "other"), 9: V(True, 9)}
    m = cmp.agreement(judge, ref)
    assert m["n"] == 4 and m["confusion"] == {"tp": 1, "fp": 1, "fn": 1, "tn": 1}
    assert m["accuracy"] == 0.5 and m["kappa"] == 0.0
    assert m["game_agree"] == 0.75 and m["game_agree_cs2_as_other"] == 1.0
    assert m["score_mae"] == 1.5 and m["score_spearman"] == pytest.approx(0.8)
    perfect = cmp.agreement(ref, ref)
    assert perfect["accuracy"] == 1.0 and perfect["kappa"] == 1.0
    assert cmp.agreement({}, ref) == {"n": 0}
    assert cmp.threshold_sweep(judge, ref)[7] == 0.75


def test_combine_rejudge_replaces_only_borderline():
    haiku = {1: V(True, 8), 2: V(True, 6), 3: V(False, 4), 4: V(False, 2)}
    out = cmp.combine_rejudge(haiku, {2: V(False, 3)})
    assert out == {1: haiku[1], 2: V(False, 3), 4: haiku[4]}  # 3 is borderline with no rejudge: unknown


def _cands(n_per: int = 5) -> list[dict]:
    out, i = [], 0
    for game in ("tarkov", "other"):
        for keep in (True, False):
            for k in range(n_per):
                i += 1
                out.append({"id": i, "haiku": V(keep, (6 + k % 3) if keep else (2 + k % 4), game)})
    return out


def test_pick_sample_is_stratified_and_prefix_balanced():
    cands = _cands()
    s = cmp.pick_sample(cands, 9)
    assert len(s) == 9 and len({c["id"] for c in s}) == 9
    strata = [(c["haiku"]["game"] == "tarkov", c["haiku"]["keep"]) for c in s]
    assert all(strata.count(k) in (2, 3) for k in set(strata)) and len(set(strata)) == 4
    assert len(set(strata[:4])) == 4  # interleaved: any prefix covers the strata evenly
    assert cmp.pick_sample(cands, 9) == s  # deterministic
    assert len(cmp.pick_sample(cands, 100)) == len(cands)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    s = dataclasses.replace(
        Settings(), data_dir=tmp_path / "data", ledger_path=tmp_path / "data" / "ledger.sqlite",
        needs_jay_path=tmp_path / "NEEDS-JAY.md",
    )  # fmt: skip
    tl = tmp_path / "tl.json"
    tl.write_text(json.dumps({"duration": 90, "events": [], "transcript": []}))
    with Store(s) as st:
        rid = st.upsert_replay(tmp_path / "Replay x.mp4", "abc", 1, 1.0)["id"]
        st.update_replay(rid, timeline_path=str(tl))
        cands = _cands(2)
        st.add_candidates(
            rid, [{"start": c["id"] * 10.0, "end": c["id"] * 10.0 + 8, "score": 1.0} for c in cands]
        )
        for row, c in zip(st.candidates(rid), cands, strict=True):
            v = {**c["haiku"], "title": "t", "summary": "s", "start": 0, "end": 1, "thumb_t": 0}
            st.set_judgement(row["id"], "judged", verdict=v, model="claude-haiku-5-5")
        st.label_candidate(1, "down")
        st.label_candidate(2, "up")

    calls = []

    def fake_claude(ctx, out, role, model, system, extra="", attempts=2):
        calls.append((role, model, ctx.cid, attempts, bool(extra)))
        out.ledger_rows.append(len(calls))
        out.attempts.append(f"{model} #1: ok")
        out.input_tokens, out.output_tokens = 1000, 100
        return {**V(True, 7, "cs2"), "title": "t", "summary": "s", "start": ctx.start, "end": ctx.end,
                "thumb_t": ctx.start}  # fmt: skip

    monkeypatch.setattr(cmp, "_claude", fake_claude)
    monkeypatch.setattr(
        cmp, "ollama_judge",
        lambda s, system, text, images, schema: OllamaResult(
            ok=True, model="qwen3:8b", duration_s=1.5,
            data={**V(False, 3), "title": "t", "summary": "s", "start": 0, "end": 0, "thumb_t": 0}),
    )  # fmt: skip
    return s, calls


def _args(**kw):
    d = dict(
        reference=8, rejudge=8, haiku_repeat=0, ollama=False, ollama_vision=0, max_sonnet=30, max_haiku=20
    )
    return argparse.Namespace(**{**d, **kw})


def test_run_respects_budget_and_is_resumable(setup, monkeypatch):
    s, calls = setup
    monkeypatch.setattr("autoclipper.gpu_gate.GpuGate.games_running", lambda self: False)
    cmp.cmd_run(_args(max_sonnet=5, ollama=True), s)
    assert len(calls) == 5 and all(c[0] == "polish" and c[1] == s.polish_model and c[3] == 1 for c in calls)
    db = cmp.CompareDB(s.data_dir / cmp.DB_NAME)
    assert db.calls(cmp.SONNET_JUDGES) == 5 and len(db.verdicts("ollama")) == 8
    cmp.cmd_run(_args(max_sonnet=5), s)  # budget spent: no more calls
    assert len(calls) == 5
    cmp.cmd_run(_args(max_sonnet=30, haiku_repeat=2), s)  # resumes, never repeats a stored verdict
    n_border = sum(cmp.borderline(c["haiku"]) for c in _cands(2))
    assert db.calls(cmp.SONNET_JUDGES) == 8 + n_border and db.calls(cmp.HAIKU_JUDGES) == 2
    assert sum(c[4] for c in calls) == n_border  # rejudge calls carry the first-pass verdict


def test_run_stops_when_a_game_is_running(setup, monkeypatch):
    s, _ = setup
    monkeypatch.setattr("autoclipper.gpu_gate.GpuGate.games_running", lambda self: True)
    cmp.cmd_run(_args(reference=0, ollama=True), s)
    assert cmp.CompareDB(s.data_dir / cmp.DB_NAME).verdicts("ollama") == {}


def test_report_against_sonnet_and_labels(setup, monkeypatch, capsys):
    s, _ = setup
    monkeypatch.setattr("autoclipper.gpu_gate.GpuGate.games_running", lambda self: False)
    cmp.cmd_run(_args(ollama=True), s)
    cmp.cmd_report(argparse.Namespace(against="sonnet", out=None), s)
    rep = json.loads((s.data_dir / "judge-compare-sonnet.json").read_text())
    assert rep["reference_n"] == 8 and rep["agreement"]["haiku"]["n"] == 8
    assert rep["agreement"]["ollama"]["keep_rate"] == 0.0 and rep["cost"]["sonnet"]["calls"] == 8
    assert "| haiku+rejudge |" in capsys.readouterr().out
    cmp.cmd_report(argparse.Namespace(against="labels", out=None), s)
    rep = json.loads((s.data_dir / "judge-compare-labels.json").read_text())
    assert rep["reference_n"] == 2 and rep["agreement"]["sonnet"]["n"] == 2


def test_keep_threshold_overrides_model_flag():
    from autoclipper.judging import JudgeOutcome, apply_keep_threshold

    out = JudgeOutcome(verdict={"keep": True, "score": 5}, source="claude-haiku")
    v = apply_keep_threshold(out, 7).verdict
    assert v["keep"] is False and v["model_keep"] is True
    assert apply_keep_threshold(JudgeOutcome(verdict={"keep": False, "score": 8}), 7).verdict["keep"] is True
    assert apply_keep_threshold(out, 0) is out
    assert apply_keep_threshold(JudgeOutcome(verdict={}), 7).verdict == {}
