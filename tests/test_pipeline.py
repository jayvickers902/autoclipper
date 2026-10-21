from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from autoclipper import judging
from autoclipper.candidates import Candidate
from autoclipper.cli import main
from autoclipper.config import Settings
from autoclipper.judging import JudgeOutcome
from autoclipper.pipeline import Context, Options, process_pass, process_replay
from autoclipper.report import build_report
from autoclipper.safety import WriteRefused, assert_writable
from autoclipper.scan import StabilityTracker, candidate_files, settle
from autoclipper.store import SCHEMA_VERSION, Store

NAME = "Replay 2026-01-01 10-00-00.mp4"


class CountingJudge:
    """Fake judge: counts calls; can be told to queue, fail, or report a pause."""

    def __init__(self, mode: str = "keep", paused: str | None = None):
        self.mode, self.pause, self.calls = mode, paused, 0

    def paused(self) -> str | None:
        return self.pause

    def __call__(self, replay: Path, candidate: Candidate, timeline: dict) -> JudgeOutcome:
        self.calls += 1
        if self.mode == "raise":
            raise RuntimeError("boom")
        if self.mode == "queue":
            return JudgeOutcome(verdict={}, queued=True, error="claude paused until 05:00")
        verdict = {
            "keep": self.mode == "keep", "score": 8, "type": "firefight", "title": "t", "summary": "s",
            "start": candidate.start + 1, "end": min(candidate.end, candidate.start + 9),
            "thumb_t": candidate.peak_t, "game": "tarkov",
        }  # fmt: skip
        return JudgeOutcome(verdict=verdict, source="fake", model="fake-model", cost_usd=0.01)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    replays = tmp_path / "replays"
    replays.mkdir()
    # like the real layout: output_dir is a subfolder of the replay dir
    return Settings(replay_dir=replays, output_dir=replays / "autoclipper", data_dir=tmp_path / "data")


@pytest.fixture
def ctx(settings: Settings):
    store = Store(settings)
    yield Context(settings, store, CountingJudge(), Options(whisper=False))
    store.close()


def put(settings: Settings, fixtures_dir: Path, fixture: str, name: str = NAME) -> Path:
    dest = settings.replay_dir / name
    shutil.copy(fixtures_dir / fixture, dest)
    return dest


def tree_hashes(root: Path) -> dict[str, str]:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob("Replay *.mp4"))}


# safety


def test_assert_writable(settings: Settings, tmp_path: Path) -> None:
    ok = [settings.output_dir / "x" / "a.mp4", settings.data_dir / "autoclipper.sqlite", settings.output_dir]
    for p in ok:
        assert assert_writable(p, settings) == p.resolve()
    bad = [
        settings.replay_dir / NAME,
        settings.replay_dir,
        tmp_path / "elsewhere.txt",
        settings.output_dir / ".." / NAME,
        settings.output_dir.parent,
    ]
    for p in bad:
        with pytest.raises(WriteRefused):
            assert_writable(p, settings)


def test_assert_writable_refuses_roots_covering_replay_dir(tmp_path: Path) -> None:
    s = Settings(replay_dir=tmp_path / "r", output_dir=tmp_path / "r", data_dir=tmp_path / "d")
    with pytest.raises(WriteRefused):
        assert_writable(tmp_path / "r" / "autoclipper" / "a.mp4", s)
    s = Settings(replay_dir=tmp_path / "r" / "x", output_dir=tmp_path, data_dir=tmp_path / "d")
    with pytest.raises(WriteRefused):
        assert_writable(tmp_path / "ok", s)


def test_store_refuses_data_dir_outside_roots(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(WriteRefused):
        Store(settings, path=settings.replay_dir / "x.sqlite")


# store


def test_schema_idempotent_and_versioned(settings: Settings) -> None:
    with Store(settings) as a:
        assert a.version == SCHEMA_VERSION
        tables = {r[0] for r in a.con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"replays", "stages", "candidates", "clips", "labels"} <= tables
    with Store(settings) as b:  # reopening changes nothing
        assert b.version == SCHEMA_VERSION
        b.con.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="newer"):
        Store(settings)


def test_label_constraint(ctx: Context) -> None:
    rid = ctx.store.upsert_replay("/x/Replay a.mp4", "ab", 1, 1.0)["id"]
    ctx.store.add_candidates(rid, [{"start": 0.0, "end": 5.0, "score": 1.0}])
    cid = ctx.store.candidates(rid)[0]["id"]
    clip = ctx.store.save_clip(cid, status="ok")
    assert ctx.store.add_label(clip, "up") > 0
    with pytest.raises(sqlite3.IntegrityError):
        ctx.store.add_label(clip, "sideways")


# pipeline


def test_process_keeps_and_is_idempotent(ctx: Context, settings: Settings, fixtures_dir: Path) -> None:
    src = put(settings, fixtures_dir, "firefight.mp4")
    before = tree_hashes(settings.replay_dir)
    res = process_replay(ctx, src)
    assert res.status == "done" and res.candidates == 1 and res.keeps == 1 and res.clips == 1
    out = settings.output_dir / src.stem
    files = sorted(p.name for p in out.iterdir())
    assert sorted(Path(f).suffix for f in files) == [".jpg", ".mp4", ".mp4", ".webp"]
    assert all(p.stat().st_size > 0 for p in out.iterdir())
    cand = ctx.store.candidates(res.replay_id)[0]
    assert cand["judge_status"] == "judged" and cand["keep"] == 1 and cand["model"] == "fake-model"
    assert cand["cost_usd"] == pytest.approx(0.01) and cand["attempts"] == 1
    clip = ctx.store.clip_for(cand["id"])
    assert clip and clip["status"] == "ok" and clip["posted"] == 0
    assert Path(clip["share_path"]).parent == out
    stages = ctx.store.stages(res.replay_id)
    assert set(stages) == {"probe", "dead_check", "analyze", "candidates", "judge", "render"}
    assert all(s["status"] == "ok" and s["seconds"] >= 0 for s in stages.values())
    timeline = Path(ctx.store.replay(res.replay_id)["timeline_path"])
    assert timeline.is_relative_to(settings.data_dir) and json.loads(timeline.read_text())["version"] == 2

    mtimes = {p: p.stat().st_mtime_ns for p in out.iterdir()}
    again = process_replay(ctx, src)
    assert again.skipped and again.status == "done"
    assert ctx.judge.calls == 1
    assert {p: p.stat().st_mtime_ns for p in out.iterdir()} == mtimes
    assert tree_hashes(settings.replay_dir) == before  # original untouched
    assert sorted(settings.replay_dir.iterdir()) == sorted([src, settings.output_dir])


def test_rejected_candidate_makes_no_clip(ctx: Context, settings: Settings, fixtures_dir: Path) -> None:
    ctx.judge.mode = "reject"
    res = process_replay(ctx, put(settings, fixtures_dir, "firefight.mp4"))
    assert res.status == "done" and res.keeps == 0 and res.clips == 0
    assert not settings.output_dir.exists()


def test_dead_capture_recorded_and_skipped(ctx: Context, settings: Settings, fixtures_dir: Path) -> None:
    res = process_replay(ctx, put(settings, fixtures_dir, "black.mp4"))
    assert res.status == "dead"
    row = ctx.store.replay(res.replay_id)
    assert row["dead"] == 1 and row["dead_reason"] and row["status"] == "dead"
    assert set(ctx.store.stages(res.replay_id)) == {"probe", "dead_check"}
    assert ctx.judge.calls == 0 and not settings.output_dir.exists()
    assert process_replay(ctx, settings.replay_dir / NAME).skipped


def test_no_candidates_is_done(ctx: Context, settings: Settings, fixtures_dir: Path) -> None:
    res = process_replay(ctx, put(settings, fixtures_dir, "speech.mp4"))
    assert res.status == "done" and res.candidates == 0 and ctx.judge.calls == 0


def test_queued_until_claude_is_back(ctx: Context, settings: Settings, fixtures_dir: Path) -> None:
    src = put(settings, fixtures_dir, "firefight.mp4")
    ctx.judge.mode = "queue"
    res = process_replay(ctx, src)
    assert res.status == "queued" and res.queued == 1 and res.clips == 0
    assert ctx.store.candidates(res.replay_id)[0]["judge_status"] == "queued"
    assert not settings.output_dir.exists()

    ctx.judge.pause = "claude paused until 05:00"  # still paused: no call at all
    calls = ctx.judge.calls
    assert process_pass(ctx, stable_wait=0) == []
    assert ctx.judge.calls == calls

    ctx.judge.pause, ctx.judge.mode = None, "keep"  # reset passed: the next pass re-judges and renders
    results = process_pass(ctx, stable_wait=0)
    assert [r.status for r in results] == ["done"] and results[0].clips == 1
    cand = ctx.store.candidates(res.replay_id)[0]
    assert cand["judge_status"] == "judged" and cand["attempts"] == 2
    assert ctx.store.stages(res.replay_id)["judge"]["runs"] == 2


def test_paused_judge_is_not_called(ctx: Context, settings: Settings, fixtures_dir: Path) -> None:
    ctx.judge.pause = "paused"
    res = process_replay(ctx, put(settings, fixtures_dir, "firefight.mp4"))
    assert res.status == "queued" and ctx.judge.calls == 0
    assert ctx.store.candidates(res.replay_id)[0]["attempts"] == 0


def test_judge_failure_marks_failed_then_retry(ctx: Context, settings: Settings, fixtures_dir: Path) -> None:
    src = put(settings, fixtures_dir, "firefight.mp4")
    ctx.judge.mode = "raise"
    res = process_replay(ctx, src)
    assert res.status == "failed" and res.failed == 1
    assert ctx.store.candidates(res.replay_id)[0]["judge_status"] == "failed"
    # watch/backfill style: failed replays are left alone unless asked
    quiet = Context(ctx.settings, ctx.store, ctx.judge, Options(whisper=False, retry_failed=False))
    assert process_replay(quiet, src).skipped
    ctx.judge.mode = "keep"
    assert process_replay(ctx, src).status == "done"


def test_duplicate_hash_not_processed_twice(ctx: Context, settings: Settings, fixtures_dir: Path) -> None:
    first = put(settings, fixtures_dir, "firefight.mp4")
    copy = put(settings, fixtures_dir, "firefight.mp4", "Replay 2026-01-01 10-05-00.mp4")
    assert process_replay(ctx, first).status == "done"
    res = process_replay(ctx, copy)
    assert res.status == "duplicate" and res.skipped
    assert ctx.store.replay(res.replay_id)["duplicate_of"] is not None
    assert ctx.judge.calls == 1
    assert [p.name for p in settings.output_dir.iterdir()] == [first.stem]


def test_pass_scans_only_top_level_replays(ctx: Context, settings: Settings, fixtures_dir: Path) -> None:
    put(settings, fixtures_dir, "firefight.mp4")
    put(settings, fixtures_dir, "four_tracks.mp4", "NotAReplay.mp4")
    put(settings, fixtures_dir, "four_tracks.mp4", "Replay 2026-01-02 notes.txt")
    (settings.replay_dir / "sub").mkdir()
    put(settings, fixtures_dir, "four_tracks.mp4", "sub/Replay 2026-01-03 00-00-00.mp4")
    settings.output_dir.mkdir()
    put(settings, fixtures_dir, "four_tracks.mp4", "autoclipper/Replay 2026-01-04 00-00-00.mp4")
    results = process_pass(ctx, stable_wait=0)
    assert [r.path.name for r in results] == [NAME]
    assert [p.name for p in candidate_files(settings.replay_dir, settings.output_dir)] == [NAME]


def test_pass_limit_and_unchanged_second_pass(ctx: Context, settings: Settings, fixtures_dir: Path) -> None:
    for i in range(3):
        put(settings, fixtures_dir, "speech.mp4", f"Replay 2026-01-0{i + 1} 10-00-00.mp4")
        # identical bytes would be duplicates; make each distinct
        with (settings.replay_dir / f"Replay 2026-01-0{i + 1} 10-00-00.mp4").open("ab") as f:
            f.write(bytes([i]) * 16)
    assert len(process_pass(ctx, limit=2, stable_wait=0)) == 2
    assert len(process_pass(ctx, stable_wait=0)) == 1
    assert process_pass(ctx, stable_wait=0) == []


# stability


def test_stability_tracker_needs_two_equal_observations(tmp_path: Path) -> None:
    f = tmp_path / "Replay a.mp4"
    f.write_bytes(b"x" * 10)
    t = StabilityTracker()
    assert t.observe([f]) == []
    assert t.observe([f]) == [f]
    f.write_bytes(b"x" * 20)
    assert t.observe([f]) == []
    assert t.observe([f]) == [f]
    empty = tmp_path / "Replay b.mp4"
    empty.write_bytes(b"")
    t.observe([empty])
    assert t.observe([empty]) == []


def test_settle_rejects_a_file_that_grows(tmp_path: Path) -> None:
    growing, still = tmp_path / "Replay g.mp4", tmp_path / "Replay s.mp4"
    growing.write_bytes(b"x")
    still.write_bytes(b"x")

    def sleep(_s: float) -> None:
        with growing.open("ab") as f:
            f.write(b"more")

    assert settle([growing, still], 5, sleep) == [still]


def test_unstable_file_not_processed(ctx: Context, settings: Settings, fixtures_dir: Path) -> None:
    put(settings, fixtures_dir, "firefight.mp4")
    assert process_pass(ctx, tracker=StabilityTracker()) == []  # first sighting only


# adaptor


def test_to_outcome_accepts_verdict_dataclass_and_wrapper() -> None:
    v = {"keep": True, "score": 7, "start": 1.0, "end": 5.0, "thumb_t": 2.0}

    class Wrapped:
        verdict = v
        queued = False
        cost_usd = 0.5
        model = "claude-haiku-5-5"
        source = "claude-haiku"

    out = judging.to_outcome(Wrapped())
    assert out.keep and out.cost_usd == 0.5 and out.model == "claude-haiku-5-5" and not out.queued
    assert judging.to_outcome({**v, "queued": True, "error": "paused"}).queued
    assert judging.to_outcome({**v, "error": "bad json"}).error == "bad json"


def test_resolve_judge_default_is_lazy(settings: Settings) -> None:
    assert isinstance(judging.resolve_judge(settings, "fake"), judging.FakeJudge)
    claude = judging.resolve_judge(settings)  # must not import or call autoclipper.judge yet
    assert isinstance(claude, judging.ClaudeJudge)


# CLI


def test_cli_process_report_backfill_watch(
    settings: Settings,
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AUTOCLIPPER_REPLAY_DIR", str(settings.replay_dir))
    monkeypatch.setenv("AUTOCLIPPER_OUTPUT_DIR", str(settings.output_dir))
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(settings.data_dir))
    monkeypatch.setattr("autoclipper.config.load_dotenv", lambda *a, **k: None)
    src = put(settings, fixtures_dir, "firefight.mp4")
    flags = ["--judge", "fake", "--no-whisper", "--stable-wait", "0"]

    assert main(["process", str(src), *flags]) == 0
    assert "done" in capsys.readouterr().out
    assert len(list((settings.output_dir / src.stem).iterdir())) == 4

    other = put(settings, fixtures_dir, "four_tracks.mp4", "Replay 2026-01-02 10-00-00.mp4")
    assert main(["backfill", str(settings.replay_dir), "--limit", "5", *flags]) == 0
    assert "1 replay(s) processed" in capsys.readouterr().out
    assert (settings.output_dir / other.stem).is_dir()

    assert main(["watch", "--once", *flags]) == 0
    assert "nothing to do" in capsys.readouterr().out

    assert main(["report"]) == 0
    text = capsys.readouterr().out
    assert "2 done" in text and "keeps:      2" in text and "judge:      2 calls" in text
    assert main(["report", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["clips"] == {"rendered": 2, "posted": 0} and data["stages"]["render"]["n"] == 2
    assert main(["process", str(settings.output_dir / src.stem / "x.mp4"), *flags]) == 2


def test_report_counts(ctx: Context, settings: Settings, fixtures_dir: Path) -> None:
    process_replay(ctx, put(settings, fixtures_dir, "firefight.mp4"))
    rep = build_report(ctx.store)
    assert rep["replays"] == {"done": 1} and rep["candidates"] == {"judged": 1} and rep["keeps"] == 1
    assert rep["judge"]["calls"] == 1 and rep["judge"]["by_model"] == {"fake-model": 1}
    assert set(rep["stages"]) == {"probe", "dead_check", "analyze", "candidates", "judge", "render"}


@pytest.mark.real_media
def test_real_replay_read_only(ctx: Context, settings: Settings) -> None:
    src_dir = Path("/mnt/c/Users/jayvi/Videos")
    replays = sorted(src_dir.glob("Replay *.mp4"))
    if not replays:
        pytest.skip("no real replays")
    real = replays[0]
    before = (real.stat().st_size, real.stat().st_mtime_ns)
    res = process_replay(ctx, real)
    assert res.status in {"done", "dead"}
    assert (real.stat().st_size, real.stat().st_mtime_ns) == before
    assert not any(p.is_relative_to(src_dir) for p in settings.data_dir.rglob("*"))
    if res.clips:
        assert all(p.is_relative_to(settings.output_dir) for p in settings.output_dir.rglob("*"))


def test_tags_option_feeds_the_tagger_into_the_timeline(
    ctx: Context, settings: Settings, fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoclipper import pipeline

    class Stub:
        name = "stub"

        def __call__(self, samples, sr, role):
            return [{"gunshot": 0.5}] * 20

    monkeypatch.setattr(pipeline, "_tagger", lambda: Stub())
    tagged = Context(ctx.settings, ctx.store, ctx.judge, Options(whisper=False, tags=True))
    res = process_replay(tagged, put(settings, fixtures_dir, "firefight.mp4"))
    tl = json.loads(Path(ctx.store.replay(res.replay_id)["timeline_path"]).read_text())
    assert tl["tagger"] == "stub" and tl["seconds"]["mix"][3]["tags"] == {"gunshot": 0.5}
