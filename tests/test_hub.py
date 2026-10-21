from __future__ import annotations

import http.client
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from autoclipper.config import Settings
from autoclipper.discord import PostResult
from autoclipper.gpu_gate import GpuGate
from autoclipper.hub import post_new_clips, run_hub
from autoclipper.judging import FakeJudge
from autoclipper.pipeline import Context, ReplayResult, render_clip
from autoclipper.store import Store


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    replays = tmp_path / "replays"
    replays.mkdir()
    return Settings(
        replay_dir=replays, output_dir=tmp_path / "out", data_dir=tmp_path / "data", port=0,
        ledger_path=tmp_path / "ledger.sqlite",
    )  # fmt: skip


@pytest.fixture
def ctx(settings: Settings):
    logs: list[str] = []
    with Store(settings) as store:
        c = Context(settings=settings, store=store, judge=FakeJudge(), log=logs.append)
        c.logs = logs  # type: ignore[attr-defined]
        yield c


def get(port: int, path: str) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


def test_run_hub_serves_and_stops(ctx: Context):
    stop, ready = threading.Event(), threading.Event()
    ports: list[int] = []
    code: list[int] = []

    def on_ready(p: int) -> None:
        ports.append(p)
        ready.set()

    t = threading.Thread(
        target=lambda: code.append(run_hub(ctx, stop=stop, interval=0.05, stable_wait=0, on_ready=on_ready))
    )
    t.start()
    assert ready.wait(10)
    status, body = get(ports[0], "/")
    assert status == 200 and b"autoclipper" in body.lower()
    time.sleep(0.2)
    stop.set()
    t.join(10)
    assert not t.is_alive() and code == [0]
    assert any(line.startswith("pass: 0 replay(s) (idle)") for line in ctx.logs)  # type: ignore[attr-defined]
    with pytest.raises(OSError):
        get(ports[0], "/")


def _seed_clip(ctx: Context, tmp_path: Path, n: int = 2) -> int:
    s = ctx.settings
    rid = ctx.store.upsert_replay(s.replay_dir / "Replay A.mp4", "a" * 64, 10, 1.0)["id"]
    ctx.store.add_candidates(rid, [{"start": 10.0 * i, "end": 10.0 * i + 8, "score": 0.9} for i in range(n)])
    for c in ctx.store.candidates(rid):
        share = tmp_path / f"share{c['idx']}.mp4"
        share.write_bytes(b"x")
        ctx.store.set_judgement(c["id"], "judged", verdict={
            "keep": True, "title": f"T{c['idx']}", "game": "tarkov", "summary": "s",
            "start": c["start"], "end": c["end"],
        })  # fmt: skip
        ctx.store.save_clip(c["id"], status="ok", share_path=str(share))
    return rid


class FakePoster:
    def __init__(self, results: list[PostResult]) -> None:
        self.results, self.calls = list(results), []

    def post_clip(self, path, title, game, length_s, summary=""):
        self.calls.append((Path(path).name, title, game, length_s))
        return self.results.pop(0)


def test_post_new_clips_marks_posted(ctx: Context, tmp_path: Path):
    rid = _seed_clip(ctx, tmp_path)
    res = [ReplayResult(Path("Replay A.mp4"), "done", rid, clips=2)]
    poster = FakePoster([PostResult("posted"), PostResult("posted")])
    assert post_new_clips(ctx, res, poster) == 2
    assert poster.calls[0] == ("share0.mp4", "T0", "tarkov", 8.0)
    assert ctx.store.unposted_clips(rid) == []
    assert post_new_clips(ctx, res, poster) == 0 and len(poster.calls) == 2


def test_post_stops_at_cap_and_keeps_clip_unposted(ctx: Context, tmp_path: Path):
    rid = _seed_clip(ctx, tmp_path)
    res = [ReplayResult(Path("Replay A.mp4"), "done", rid, clips=2)]
    poster = FakePoster([PostResult("posted"), PostResult("refused", "clip cap reached")])
    assert post_new_clips(ctx, res, poster) == 1
    assert len(ctx.store.unposted_clips(rid)) == 1


def test_render_uses_gate_encoder(ctx: Context, tmp_path: Path, monkeypatch):
    seen: dict[str, str] = {}

    def fake_encode(replay, start, end, out, max_mb, encoder):
        seen["encoder"] = encoder
        raise RuntimeError("stop here")

    monkeypatch.setattr("autoclipper.pipeline.media.cut_copy", lambda *a, **k: None)
    monkeypatch.setattr("autoclipper.pipeline.media.share_encode", fake_encode)
    rid = _seed_clip(ctx, tmp_path, 1)
    cand = ctx.store.candidates(rid)[0]
    games = tmp_path / "games.txt"
    games.write_text("cs2.exe\n")
    for procs, expected in (({"cs2"}, "libx264"), (set(), "auto")):
        ctx.gpu_gate = GpuGate(games, processes=lambda p=procs: p)
        with pytest.raises(RuntimeError):
            render_clip(ctx, ctx.settings.replay_dir / "Replay A.mp4", cand)
        assert seen["encoder"] == expected


def test_run_command_starts_serves_and_exits_on_sigterm(tmp_path: Path):
    (tmp_path / "replays").mkdir()
    env = {
        **os.environ,
        "AUTOCLIPPER_DATA_DIR": str(tmp_path / "data"),
        "AUTOCLIPPER_OUTPUT_DIR": str(tmp_path / "out"),
        "AUTOCLIPPER_REPLAY_DIR": str(tmp_path / "replays"),
        "AUTOCLIPPER_LEDGER": str(tmp_path / "ledger.sqlite"),
        "AUTOCLIPPER_GAMES_FILE": str(tmp_path / "games.txt"),
        "DISCORD_TEST_WEBHOOK_URL": "",
    }  # fmt: skip
    code = "import sys; from autoclipper.cli import main; sys.exit(main(sys.argv[1:]))"
    proc = subprocess.Popen(
        [sys.executable, "-c", code, "run", "--judge", "fake", "--port", "0", "--interval", "0.1"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip
    try:
        port = None
        lines: list[str] = []
        assert proc.stdout
        for line in proc.stdout:
            lines.append(line)
            if m := re.search(r"review on http://127\.0\.0\.1:(\d+)/", line):
                port = int(m[1])
                break
        assert port, "".join(lines)
        assert get(port, "/")[0] == 200
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 0
        assert "stopping" in "".join(lines) + proc.stdout.read()
    finally:
        if proc.poll() is None:
            proc.kill()
