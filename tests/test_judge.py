"""Judge, guard and ledger tests. A fake `claude` replays recorded stream-json; the real CLI never runs."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import autoclipper.judge.core as core
import autoclipper.judge.guard as guard_mod
from autoclipper.config import PROJECT_ROOT, Settings
from autoclipper.judge import Guard, GuardRefused, LiveCallInTests, Verdict, judge_candidate, polish
from autoclipper.judge.claude_cli import CallResult, RateLimit, minimal_env, parse_stream, run_claude
from autoclipper.judge.frames import extract_frames, frame_times
from autoclipper.judge.schema import SchemaError, validate, verdict_schema

JUDGE_FIX = Path(__file__).resolve().parent / "fixtures" / "judge"
CAND = {"start": 41.5, "end": 85.75, "score": 2.98, "reasons": ["fight:13 shots"], "peak_t": 82.5}
TIMELINE = {
    "version": 1, "duration": 89.3, "dead": False,
    "sources": {"transients": "mix", "speech": ["mix"]},
    "seconds": {"mix": [{"t": t, "rms_db": -30.0} for t in range(90)]},
    "events": [{"t": 46.0, "kind": "transient", "strength": 0.7, "role": "mix"},
               {"t": 67.7, "kind": "keyword", "phrase": "what the", "role": "mix"}],
    "transcript": [{"role": "mix", "start": 67.7, "end": 68.1, "text": "What the?"}],
}  # fmt: skip
VALID = {"keep": True, "score": 7, "type": "kill", "title": "t", "summary": "s", "start": 50, "end": 80,
         "thumb_t": 60, "game": "other"}  # fmt: skip


class FakeOllama:
    """Local ollama stand-in on an ephemeral port: /api/tags and /api/chat."""

    def __init__(self, models=("qwen3:8b",), answer: dict | str | None = None):
        self.models, self.answer, self.requests = list(models), answer, []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.requests.append(("GET", self.path, None))
                self._send({"models": [{"name": m} for m in fake.models]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(("POST", self.path, body))
                ans = fake.answer if isinstance(fake.answer, str) else json.dumps(fake.answer or VALID)
                self._send({"message": {"role": "assistant", "content": ans}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _closed_port_url() -> str:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


class Env:
    def __init__(self, tmp: Path):
        self.state = tmp / "fake-state"
        self.state.mkdir()
        self.ledger = tmp / "data" / "ledger.sqlite"
        wrapper = tmp / "bin" / "fake-claude"
        wrapper.parent.mkdir()
        wrapper.write_text(
            "#!/bin/sh\n# AUTOCLIPPER_FAKE_CLAUDE\n"
            f'exec "{sys.executable}" "{JUDGE_FIX / "fake_claude.py"}" "{self.state}" "$@"\n'
        )
        wrapper.chmod(0o755)
        self.settings = dataclasses.replace(
            Settings(), claude_bin=str(wrapper), ledger_path=self.ledger, needs_jay_path=tmp / "NEEDS-JAY.md",
            judge_timeout_s=20.0, ollama_url=_closed_port_url(),
        )  # fmt: skip
        self.plan(["success"])

    def plan(self, scenarios: list[str]) -> None:
        (self.state / "plan.json").write_text(
            json.dumps({"scenarios": scenarios, "ledger": str(self.ledger)})
        )

    def calls(self) -> list[dict]:
        files = sorted(self.state.glob("call-*.json"), key=lambda p: int(p.stem.split("-")[1]))
        return [json.loads(p.read_text()) for p in files]

    def rows(self) -> list[dict]:
        con = sqlite3.connect(self.ledger)
        con.row_factory = sqlite3.Row
        out = [dict(r) for r in con.execute("SELECT * FROM calls ORDER BY id")]
        con.close()
        return out

    def guard(self) -> Guard:
        return Guard(self.settings)

    def judge(self, **kw):
        s = kw.pop("settings", self.settings)
        return judge_candidate("/nonexistent/replay.mp4", CAND, TIMELINE, s, **kw)


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    monkeypatch.setattr(core, "extract_frames", lambda path, times: [(t, "AAAA") for t in times])
    return Env(tmp_path)


# --- success path, ledger, child process ------------------------------------------------------------


def test_success_verdict_and_ledger(env):
    r = env.judge()
    v = r.verdict
    assert (v.source, v.model, v.queued, v.keep, v.score, v.game) == (
        "claude-haiku", "claude-haiku-5-5", False, True, 8, "other",
    )  # fmt: skip
    assert v.start == 64 and v.end == 85 and len(v.title) <= 60
    assert r.ledger_rows == [1] and r.cost_usd == pytest.approx(0.0028943)
    assert r.input_tokens == 11680 and r.output_tokens == 1117
    (row,) = env.rows()
    assert row["status"] == "ok" and row["role"] == "judge" and row["model"] == "claude-haiku-5-5"
    assert row["five_hour"] == 0.22 and row["seven_day"] == 0.23 and row["rl_status"] == "allowed"
    assert row["cost_usd"] == pytest.approx(0.0028943) and row["duration_s"] > 0
    assert not env.guard().status()["paused"]


def test_ledger_row_reserved_before_call_and_child_isolated(env, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    env.judge()
    (call,) = env.calls()
    assert call["started_rows"] == 1  # the row existed while the CLI ran
    assert set(call["env"]) <= {"HOME", "PATH", "PWD", "SHLVL", "_", "LC_CTYPE"}  # sh, python add these
    assert not any(k.startswith(("ANTHROPIC", "CLAUDE")) for k in call["env"])
    assert call["cwd_entries"] == [] and call["cwd"] != os.getcwd()
    argv = call["argv"]
    for flag in ("-p", "--safe-mode", "--strict-mcp-config", "--no-session-persistence", "--verbose"):
        assert flag in argv
    assert argv[argv.index("--model") + 1] == "claude-haiku-5-5"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--input-format") + 1] == argv[argv.index("--output-format") + 1] == "stream-json"
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema["properties"]["title"]["maxLength"] == 60
    assert schema["properties"]["start"]["minimum"] == 41.5
    assert call["stdin_lines"] == 1 and 8 <= call["n_images"] <= 12


def test_minimal_env_only_home_and_path(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SSE_PORT", "1")
    assert set(minimal_env()) == {"HOME", "PATH"}


# --- failures: retry, fallback, pauses -------------------------------------------------------------


def test_error_then_success_retries_once(env):
    env.plan(["error_result", "success"])
    r = env.judge()
    assert r.verdict.source == "claude-haiku" and len(r.ledger_rows) == 2
    assert [row["status"] for row in env.rows()] == ["error", "ok"]
    assert "529" in env.rows()[0]["error"]
    assert not env.guard().status()["paused"]


def test_invalid_structured_output_counts_as_failure(env, monkeypatch):
    real = core.validate
    calls = []

    def flaky(d, s, e):
        calls.append(1)
        if len(calls) == 1:
            raise SchemaError("bad")
        return real(d, s, e)

    monkeypatch.setattr(core, "validate", flaky)
    r = env.judge()
    assert r.verdict.source == "claude-haiku"
    assert [row["status"] for row in env.rows()] == ["invalid", "ok"]


def test_two_failures_fall_back_to_ollama_and_queue(env):
    oll = FakeOllama()
    try:
        env.plan(["error_result", "no_structured"])
        s = dataclasses.replace(env.settings, ollama_url=oll.url)
        r = env.judge(settings=s)
    finally:
        oll.close()
    v = r.verdict
    assert (v.source, v.model, v.queued, v.score) == ("ollama", "qwen3:8b", True, 7)
    assert [(x["role"], x["status"]) for x in env.rows()] == [
        ("judge", "error"), ("judge", "error"), ("fallback", "ok"),
    ]  # fmt: skip
    post = [b for m, p, b in oll.requests if m == "POST"]
    assert len(post) == 1 and post[0]["model"] == "qwen3:8b" and "images" not in post[0]["messages"][1]
    assert post[0]["format"]["required"] and post[0]["stream"] is False
    st = env.guard().status()
    assert st["paused"] and "two failed" in st["reason"]
    assert st["counts"]["haiku_total"] == 2 and st["counts"]["fallback_total"] == 1


def test_ollama_vision_sends_frames(env):
    oll = FakeOllama(models=("qwen3.8-27b:latest",))
    try:
        env.plan(["error_result"])
        s = dataclasses.replace(env.settings, ollama_url=oll.url, ollama_vision=True)
        r = env.judge(settings=s)
    finally:
        oll.close()
    assert r.verdict.model == "qwen3.8-27b"
    (post,) = [b for m, p, b in oll.requests if m == "POST"]
    assert len(post["messages"][1]["images"]) >= 8


def test_ollama_missing_model_is_unavailable_and_never_pulled(env):
    oll = FakeOllama(models=("llama3:8b",))
    try:
        env.plan(["error_result"])
        r = env.judge(settings=dataclasses.replace(env.settings, ollama_url=oll.url))
    finally:
        oll.close()
    v = r.verdict
    assert v.source == "none" and v.queued and not v.keep and "not installed" in v.error
    assert v.start == CAND["start"] and v.end == CAND["end"]
    assert all(p == "/api/tags" for _, p, _ in oll.requests)  # no /api/pull, no /api/chat
    assert env.rows()[-1]["status"] == "unavailable"


def test_ollama_unreachable_is_unavailable(env):
    env.plan(["error_result"])
    r = env.judge()
    assert r.verdict.source == "none" and r.verdict.queued and "unreachable" in r.verdict.error


def test_rejected_pauses_until_reset_then_resumes(env, monkeypatch):
    env.plan(["rejected", "success"])
    t0 = time.time()
    r = env.judge()
    assert len(env.calls()) == 1  # the pause stops the retry
    assert r.verdict.source == "none" and r.verdict.queued
    st = env.guard().status()
    assert st["paused"] and "rejected" in st["reason"]
    assert st["until_epoch"] == pytest.approx(t0 + 7200, abs=5)
    # Paused: the next candidate never reaches the CLI.
    r2 = env.judge()
    assert len(env.calls()) == 1 and "paused until" in r2.attempts[0]
    # After resetsAt, calls resume.
    later = time.time() + 7300
    monkeypatch.setattr(guard_mod.time, "time", lambda: later)
    assert env.guard().check("judge", "claude-haiku-5-5") is None
    assert env.judge().verdict.source == "claude-haiku"


def test_rejection_pause_is_at_least_15_minutes(env):
    env.plan(["rejected_stale_reset"])
    t0 = time.time()
    env.judge()
    assert env.guard().status()["until_epoch"] >= t0 + 15 * 60


def test_usage_limit_text_detected():
    res = CallResult(result_text="You've hit your session limit · resets 3:40am (America/Halifax)")
    assert res.limit_hit
    assert CallResult(api_error="rate_limit").limit_hit
    assert not CallResult(ok=True, result_text="usage limit").limit_hit


def test_five_hour_threshold_pauses_at_five_hour_reset(env):
    env.plan(["warning"])
    t0 = time.time()
    r = env.judge()
    assert r.verdict.source == "claude-haiku"  # the call itself succeeded
    assert env.rows()[0]["rl_status"] == "allowed_warning"
    st = env.guard().status()
    assert st["paused"] and "5-hour" in st["reason"]
    assert st["until_epoch"] == pytest.approx(t0 + 3000, abs=5)


def test_seven_day_threshold_pauses(env):
    env.plan(["weekly_high"])
    t0 = time.time()
    env.judge()
    st = env.guard().status()
    assert st["paused"] and "7-day" in st["reason"]
    assert st["until_epoch"] == pytest.approx(t0 + 220000, abs=5)


def test_missing_rate_limit_event_pauses(env):
    env.plan(["no_rate_limit"])
    t0 = time.time()
    assert env.judge().verdict.source == "claude-haiku"
    st = env.guard().status()
    assert st["paused"] and "rate_limit_event" in st["reason"]
    assert st["until_epoch"] >= t0 + 15 * 60


def test_timeout_kills_process_group_then_falls_back(env):
    env.plan(["hang"])
    s = dataclasses.replace(env.settings, judge_timeout_s=1.0)
    t0 = time.monotonic()
    r = env.judge(settings=s)
    assert time.monotonic() - t0 < 15
    assert [x["status"] for x in env.rows() if x["role"] == "judge"] == ["timeout", "timeout"]
    assert r.verdict.source == "none" and r.verdict.queued
    for pidfile in env.state.glob("hang-*.pid"):
        pid = int(pidfile.read_text())
        for _ in range(50):
            if not Path(f"/proc/{pid}").exists() or "Z" in Path(f"/proc/{pid}/stat").read_text().split()[2]:
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"grandchild {pid} survived the timeout")
    assert "two failed" in env.guard().status()["reason"]


def test_auth_failure_is_fatal_and_writes_needs_jay(env):
    env.plan(["auth_failure", "success"])
    r = env.judge()
    assert len(env.calls()) == 1 and r.verdict.queued
    st = env.guard().status()
    assert st["paused"] and st["fatal"] and "auth" in st["reason"]
    note = env.settings.needs_jay_path.read_text()
    assert "/login" in note and note.count(guard_mod.NEEDS_JAY_MARKER) == 1
    env.judge()  # still paused, note not duplicated
    assert env.settings.needs_jay_path.read_text().count(guard_mod.NEEDS_JAY_MARKER) == 1
    env.guard().clear_pause()
    assert env.judge().verdict.source == "claude-haiku"


def test_api_key_source_is_fatal(env):
    env.plan(["api_key_source"])
    env.judge()
    st = env.guard().status()
    assert st["fatal"] and "API key" in st["reason"]


def test_missing_binary_is_fatal_outside_pytest(env, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    s = dataclasses.replace(env.settings, claude_bin=str(env.state / "no-such-claude"))
    env.judge(settings=s)
    assert env.guard().status()["fatal"]


# --- caps and serialisation ------------------------------------------------------------------------


def test_run_caps_count_every_row_including_failures(env):
    s = dataclasses.replace(env.settings, run_max_haiku=3, run_max_sonnet=1)
    g = Guard(s)
    for _ in range(3):
        with pytest.raises(RuntimeError):  # a crashed call still counts
            with g.slot("judge", "claude-haiku-5-5"):
                raise RuntimeError("boom")
    with pytest.raises(GuardRefused, match="run cap"):
        with g.slot("judge", "claude-haiku-5-5"):
            pass
    assert [r["status"] for r in env.rows()] == ["error"] * 3
    with g.slot("polish", "claude-sonnet-5-5"):
        pass  # Sonnet has its own cap
    with pytest.raises(GuardRefused, match="run cap"):
        with g.slot("polish", "claude-sonnet-5-5"):
            pass
    assert g.status()["counts"]["haiku_total"] == 3


def test_hourly_and_daily_caps(env):
    s = dataclasses.replace(env.settings, judge_max_calls_per_hour=2, polish_max_calls_per_day=1)
    g = Guard(s)
    for _ in range(2):
        with g.slot("judge", "claude-haiku-5-5"):
            pass
    with pytest.raises(GuardRefused, match="hourly"):
        with g.slot("judge", "claude-haiku-5-5"):
            pass
    con = sqlite3.connect(env.ledger)
    con.execute("UPDATE calls SET ts = ts - 3700")
    con.commit()
    con.close()
    with g.slot("judge", "claude-haiku-5-5"):
        pass  # older rows left the window
    with g.slot("polish", "claude-sonnet-5-5"):
        pass
    with pytest.raises(GuardRefused, match="daily"):
        with g.slot("polish", "claude-sonnet-5-5"):
            pass


def test_slots_are_serialised_across_holders(env):
    g1, g2 = env.guard(), env.guard()
    order: list[str] = []
    inside = threading.Event()

    def first():
        with g1.slot("judge", "claude-haiku-5-5"):
            inside.set()
            time.sleep(0.4)
            order.append("first-done")

    t = threading.Thread(target=first)
    t.start()
    inside.wait(5)
    with g2.slot("judge", "claude-haiku-5-5"):
        order.append("second-in")
    t.join()
    assert order == ["first-done", "second-in"]


def test_pause_never_shortened_by_a_later_shorter_pause(env):
    g = env.guard()
    far = time.time() + 5000
    g.pause(far, "long")
    g.pause(time.time() + 100, "short")
    assert g.status()["until_epoch"] == pytest.approx(far)


# --- the pytest guard ------------------------------------------------------------------------------


def test_real_claude_refused_under_pytest(tmp_path):
    with pytest.raises(LiveCallInTests):
        run_claude("claude", "claude-haiku-5-5", "x", {}, [], 5)
    marker = tmp_path / "ran"
    script = tmp_path / "unmarked"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    with pytest.raises(LiveCallInTests):
        run_claude(str(script), "claude-haiku-5-5", "x", {}, [], 5)
    assert not marker.exists()
    real = shutil.which("claude")
    if real:
        with pytest.raises(LiveCallInTests):
            run_claude(real, "claude-haiku-5-5", "x", {}, [], 5)


def test_guard_refuses_real_ledger_under_pytest():
    with pytest.raises(RuntimeError, match="temporary"):
        Guard(Settings())


def test_no_anthropic_sdk_or_api_key_in_code():
    src = PROJECT_ROOT / "src"
    bad = re.compile(
        r"^\s*(import anthropic|from anthropic)|environ\[[\"']ANTHROPIC|setenv\([\"']ANTHROPIC", re.M
    )
    hits = [p for p in src.rglob("*.py") if bad.search(p.read_text())]
    assert hits == []
    assert "anthropic" not in (PROJECT_ROOT / "pyproject.toml").read_text().lower()


# --- polish / re-judge ------------------------------------------------------------------------------


def test_polish_uses_sonnet_and_polish_role(env):
    v = Verdict(**VALID, source="claude-haiku", model="claude-haiku-5-5", candidate_id="c1")
    r = polish("/nonexistent/replay.mp4", CAND, TIMELINE, v, env.settings)
    assert r.verdict.source == "claude-sonnet" and r.verdict.candidate_id == "c1"
    assert env.rows()[0]["role"] == "polish" and env.rows()[0]["model"] == "claude-sonnet-5-5"
    (call,) = env.calls()
    assert call["argv"][call["argv"].index("--model") + 1] == "claude-sonnet-5-5"


def test_polish_failure_keeps_original_verdict(env):
    env.plan(["error_result"])
    v = Verdict(**VALID, source="claude-haiku", model="claude-haiku-5-5")
    r = polish("/nonexistent/replay.mp4", CAND, TIMELINE, v, env.settings, mode="rejudge")
    assert r.verdict == v and "rejudge failed" in r.attempts[-1]


def test_rejudge_borderline(env, monkeypatch):
    real = core.validate
    monkeypatch.setattr(core, "validate", lambda d, s, e: {**real(d, s, e), "score": 5})
    r = env.judge(rejudge_borderline=True)
    assert r.verdict.source == "claude-sonnet"
    assert [x["role"] for x in env.rows()] == ["judge", "polish"]


def test_judge_setting_ollama_skips_claude(env):
    oll = FakeOllama()
    try:
        r = env.judge(settings=dataclasses.replace(env.settings, judge="ollama", ollama_url=oll.url))
    finally:
        oll.close()
    assert r.verdict.source == "ollama" and not r.verdict.queued and env.calls() == []


# --- parsing, schema, frames, config ----------------------------------------------------------------


def test_parse_real_success_fixture():
    text = (JUDGE_FIX / "success.jsonl").read_text().replace('"__T+14000__"', "1792582800")
    text = re.sub(r'"__T[+-]\d+__"', "1", text)
    res = parse_stream(text, CallResult())
    assert res.api_key_source == "none" and res.structured["game"] == "other"
    assert res.rate_limit == RateLimit("allowed", 1792582800.0, 0.22, 1792582800.0, 0.23, 1.0)
    assert res.cost_usd == pytest.approx(0.0028943) and res.usage["output_tokens"] == 1117


def test_validate_clamps_and_truncates():
    d = validate(
        {**VALID, "title": "x" * 80, "summary": "y" * 200, "start": 0, "end": 999, "thumb_t": 1}, 40, 86
    )
    assert (d["start"], d["end"], d["thumb_t"]) == (40, 86, 40) and len(d["title"]) == 60
    assert len(d["summary"]) == 140
    with pytest.raises(SchemaError):
        validate({**VALID, "type": "dance"}, 40, 86)
    with pytest.raises(SchemaError):
        validate({k: v for k, v in VALID.items() if k != "keep"}, 40, 86)
    assert verdict_schema(1, 2)["additionalProperties"] is False


def test_frame_times_count():
    assert len(frame_times(0, 10)) == 8 and len(frame_times(0, 200)) == 12
    ts = frame_times(41.5, 85.75)
    assert all(41.5 < t < 85.75 for t in ts) and ts == sorted(ts)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_extract_frames_max_1024(tmp_path):
    clip = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=10:duration=3",
         "-pix_fmt", "yuv420p", str(clip)], check=True,
    )  # fmt: skip
    import base64

    frames = extract_frames(clip, [0.5, 1.5, 2.5])
    assert len(frames) == 3
    jpg = base64.b64decode(frames[0][1])
    i = 2
    while jpg[i + 1] not in (0xC0, 0xC2):
        i += 2 + int.from_bytes(jpg[i + 2 : i + 4], "big")
    h, w = int.from_bytes(jpg[i + 5 : i + 7], "big"), int.from_bytes(jpg[i + 7 : i + 9], "big")
    assert (w, h) == (1024, 576)


def test_settings_judge_env(monkeypatch):
    env = {"AUTOCLIPPER_JUDGE_MAX_7D": "0.5", "AUTOCLIPPER_RUN_MAX_HAIKU": "7",
           "AUTOCLIPPER_CLAUDE_BIN": "/x/fake", "AUTOCLIPPER_OLLAMA_VISION": "true"}  # fmt: skip
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env(dotenv=False)
    assert (s.judge_max_7d, s.run_max_haiku, s.claude_bin, s.ollama_vision) == (0.5, 7, "/x/fake", True)
    assert s.run_max_sonnet == 40 and s.judge_timeout_s == 120
