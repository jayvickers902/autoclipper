"""Hub/watch ingest of finished inbox uploads and the `upload-serve` subcommand (P3.4/P4.1 wiring)."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from autoclipper import hub
from autoclipper import ingest as ingest_mod
from autoclipper.cli import main
from autoclipper.config import Settings
from autoclipper.hub import replay_pass
from autoclipper.ingest import inbox_uploads, ingest_inbox
from autoclipper.judging import FakeJudge
from autoclipper.pipeline import Context, Options, ReplayResult
from autoclipper.store import Store

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    (tmp_path / "replays").mkdir()
    return Settings(
        replay_dir=tmp_path / "replays", output_dir=tmp_path / "out", data_dir=tmp_path / "data", port=0,
        ledger_path=tmp_path / "ledger.sqlite",
    )  # fmt: skip


@pytest.fixture
def ctx(settings: Settings):
    logs: list[str] = []
    with Store(settings) as store:
        c = Context(settings, store, FakeJudge(), Options(whisper=False, encoder="libx264"), log=logs.append)
        c.logs = logs  # type: ignore[attr-defined]
        yield c


def put_upload(inbox: Path, user: str, sha: str, name: str, data: bytes = b"v", meta: bool = True) -> Path:
    d = inbox / user / sha
    d.mkdir(parents=True)
    (d / name).write_bytes(data)
    if meta:
        (d / "meta.json").write_text(json.dumps({"file": name, "sha256": sha, "size": len(data)}))
    return d / name


def test_inbox_uploads_skips_staging_and_incomplete(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    ok = put_upload(inbox, "bob", "a" * 64, "game.mp4")
    put_upload(inbox / ".staging", "bob", "b" * 64, "game.mp4")
    put_upload(inbox, "bob", "c" * 64, "game.mp4", meta=False)
    put_upload(inbox, "amy", "d" * 64, "notes.txt")
    assert inbox_uploads(inbox) == [ok]
    assert inbox_uploads(tmp_path / "missing") == []


def test_ingest_inbox_stops_between_uploads_and_skips_finished(ctx: Context, monkeypatch) -> None:
    inbox = ctx.settings.data_dir / "inbox"
    for i, user in enumerate(("amy", "bob", "cy")):
        put_upload(inbox, user, str(i) * 64, "g.mp4", b"v" * (i + 1))
    calls: list[Path] = []
    stop = {"now": False, "arm": True}

    def fake_ingest(c: Context, target: Path, iopts=None) -> list[ReplayResult]:
        calls.append(target)
        stop["now"] = stop["arm"]  # SIGTERM arrives during the first upload
        return [ReplayResult(target, "done", skipped=False)]

    monkeypatch.setattr(ingest_mod, "ingest", fake_ingest)
    ctx.should_stop = lambda: stop["now"]
    assert len(ingest_inbox(ctx)) == 1 and len(calls) == 1

    # a finished upload is skipped by a stat lookup, without calling ingest
    stop["now"] = stop["arm"] = False
    calls.clear()
    first = inbox / "amy" / ("0" * 64) / "g.mp4"
    st = first.stat()
    row = ctx.store.upsert_replay(first, "9" * 64, st.st_size, st.st_mtime)
    ctx.store.update_replay(row["id"], status="done")
    ingest_inbox(ctx)
    assert first not in calls and len(calls) == 2


def test_replay_pass_ingests_upload_once(ctx: Context, monkeypatch) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is needed")
    spec = importlib.util.spec_from_file_location("make_fixtures", FIXTURES / "make_fixtures.py")
    assert spec and spec.loader
    mf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mf)
    monkeypatch.setattr("autoclipper.media.encode.nvenc_works", lambda *a, **k: False)  # CPU only
    up = put_upload(ctx.settings.data_dir / "inbox", "bob", "a" * 64, "2026-10-12 20-30-00.mp4")
    mf.make_long(up, 60, [20, 21.2, 22.5, 24])

    first = replay_pass(ctx, None, 0)
    assert [r.status for r in first] == ["done"] and first[0].path == up
    assert ctx.store.replay(first[0].replay_id)["kind"] == "long"  # type: ignore[arg-type]
    assert replay_pass(ctx, None, 0) == []  # idempotent re-scan
    # SIGTERM already set: nothing new is started
    ctx.should_stop = lambda: True
    assert replay_pass(ctx, None, 0) == []


def test_queued_upload_skipped_while_judge_paused(ctx: Context, monkeypatch) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(ingest_mod, "ingest", lambda c, target, iopts=None: calls.append(target) or [])
    up = put_upload(ctx.settings.data_dir / "inbox", "bob", "a" * 64, "g.mp4")
    st = up.stat()
    row = ctx.store.upsert_replay(up, "a" * 64, st.st_size, st.st_mtime)
    ctx.store.update_replay(row["id"], status="queued")
    ctx.judge.paused = lambda: "run cap reached: 300/300 haiku calls"  # type: ignore[method-assign]
    assert ingest_inbox(ctx) == [] and calls == []
    ctx.judge.paused = lambda: None  # type: ignore[method-assign]
    ingest_inbox(ctx)
    assert calls == [up]


def test_replay_pass_survives_inbox_failure(ctx: Context, monkeypatch) -> None:
    def boom(c: Context) -> list[ReplayResult]:
        raise OSError("inbox unreadable")

    monkeypatch.setattr(hub, "ingest_uploads", boom)
    assert replay_pass(ctx, None, 0) == []
    assert any("inbox ingest failed: OSError" in line for line in ctx.logs)  # type: ignore[attr-defined]


def test_upload_serve_refuses_without_port(monkeypatch) -> None:
    monkeypatch.setattr("autoclipper.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("AUTOCLIPPER_UPLOAD_PORT", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["upload-serve"])
    assert "upload receiver is off" in str(exc.value)


def test_upload_serve_calls_serve_with_settings(monkeypatch) -> None:
    got: list[Settings] = []
    monkeypatch.setattr("autoclipper.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("AUTOCLIPPER_UPLOAD_PORT", "4999")
    monkeypatch.setattr("autoclipper.uploads.serve", lambda settings, log=print: got.append(settings))
    assert main(["upload-serve"]) == 0
    assert got[0].upload_port == 4999
