"""Retention plan (P4.2): tiny fake files in temp dirs with set mtimes. Report-only: nothing is deleted."""

from __future__ import annotations

import json
import os
import re
import struct
import time
from pathlib import Path

import pytest

from autoclipper import hints, retention
from autoclipper.cli import main
from autoclipper.config import Settings
from autoclipper.retention import GB, plan
from autoclipper.store import Store

NOW = 1_800_000_000.0
DAY = 86400.0


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def segra_file(path: Path, size: int, age_days: float) -> Path:
    """A fake mp4: ftyp + moov/udta/uuid(SEGRA-METADATA), padded after moov to `size` bytes."""
    meta = hints.SEGRA_BOX_ID + json.dumps({"Game": "Escape From Tarkov"}).encode()
    body = _box(b"ftyp", b"isom0000") + _box(b"moov", _box(b"udta", _box(b"uuid", meta)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body + b"\0" * max(0, size - len(body)))
    return aged(path, age_days)


def plain_file(path: Path, size: int, age_days: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return aged(path, age_days)


def aged(path: Path, age_days: float) -> Path:
    t = NOW - age_days * DAY
    os.utime(path, (t, t))
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        replay_dir=tmp_path / "replays", output_dir=tmp_path / "out", data_dir=tmp_path / "data",
        recorder_dir=tmp_path / "rec", retention_gb=2000 / GB, retention_min_age_days=7.0,
        ledger_path=tmp_path / "ledger.sqlite",
    )  # fmt: skip


@pytest.fixture
def store(settings: Settings):
    with Store(settings) as s:
        yield s


def _finish(store: Store, path: Path, status: str = "done") -> int:
    st = path.stat()
    rid = store.upsert_replay(path, "a" * 63 + str(abs(hash(path)) % 10), st.st_size, st.st_mtime)["id"]
    store.update_replay(rid, status=status)
    return rid


def _kept_clip(
    store: Store, settings: Settings, replay: Path, name: str, keep: bool, age: float
) -> list[Path]:
    plain_file(replay, 10, age)
    rid = _finish(store, replay)
    store.add_candidates(rid, [{"start": 0.0, "end": 8.0, "score": 0.9}])
    (cand,) = store.candidates(rid)
    store.set_judgement(cand["id"], "judged", verdict={"keep": keep})
    d = settings.output_dir / replay.stem
    paths = [
        plain_file(d / f"{name}_{suffix}", 100, age)
        for suffix in ("cut.mp4", "share.mp4", "thumb.jpg", "preview.webp")
    ]
    store.save_clip(
        cand["id"], status="ok", cut_path=str(paths[0]), share_path=str(paths[1]),
        thumb_path=str(paths[2]), webp_path=str(paths[3]),
    )  # fmt: skip
    return paths


def test_never_lists_obs_replays_or_non_segra_files(settings: Settings, store: Store, tmp_path: Path) -> None:
    replay_dir = settings.replay_dir
    obs = plain_file(replay_dir / "Replay 2026-01-01 10-00-00.mp4", 5000, 90)
    other = plain_file(replay_dir / "holiday.mp4", 5000, 90)
    plain_in_rec = plain_file(settings.recorder_dir / "Full Sessions" / "x" / "notes.mp4", 5000, 90)
    obs_in_rec = segra_file(settings.recorder_dir / "Replay 2026-01-01 10-00-00.mp4", 5000, 90)
    ours = segra_file(
        settings.recorder_dir / "Full Sessions" / "Tarkov" / "2026-01-01_10-00-00.mp4", 5000, 90
    )
    _finish(store, ours)

    p = plan(settings, store, NOW)
    listed = {e.path for e in p.managed}
    assert listed == {ours}
    assert [e.path for e in p.delete_order] == [ours]
    for never in (obs, other, plain_in_rec, obs_in_rec):
        assert never not in listed and never.exists()
    assert "SEGRA-METADATA" in p.delete_order[0].reason


def test_recorder_dir_unset_considers_no_recorder_files(
    settings: Settings, store: Store, tmp_path: Path
) -> None:
    from dataclasses import replace

    segra_file(tmp_path / "rec" / "a.mp4", 5000, 90)
    p = plan(replace(settings, recorder_dir=None), store, NOW)
    assert p.managed == []


def test_oldest_first_until_under_cap(settings: Settings, store: Store) -> None:
    files = {}
    for name, age in (("c.mp4", 20), ("a.mp4", 60), ("b.mp4", 30), ("d.mp4", 10)):
        f = segra_file(settings.recorder_dir / name, 1000, age)
        _finish(store, f)
        files[name] = f
    p = plan(settings, store, NOW)  # 4000 B managed, cap 2000 B
    assert p.total_bytes == 4000 and p.over_bytes == 2000
    assert [e.path.name for e in p.delete_order] == ["a.mp4", "b.mp4"]
    assert p.after_bytes == 2000
    assert plan(settings, store, NOW).to_dict()["reaches_cap"] is True


def test_under_cap_plans_nothing(settings: Settings, store: Store) -> None:
    f = segra_file(settings.recorder_dir / "a.mp4", 1000, 60)
    _finish(store, f)
    p = plan(settings, store, NOW)
    assert p.delete_order == [] and p.over_bytes == 0


def test_kept_recent_and_unprocessed_are_never_candidates(settings: Settings, store: Store) -> None:
    kept = _kept_clip(store, settings, settings.replay_dir / "Replay A.mp4", "clip01_0000s", True, 90)
    dropped = _kept_clip(store, settings, settings.replay_dir / "Replay B.mp4", "clip01_0000s", False, 90)
    recent = segra_file(settings.recorder_dir / "recent.mp4", 1000, 2)
    _finish(store, recent)
    fresh_unprocessed = segra_file(settings.recorder_dir / "raw.mp4", 1000, 90)  # no store row
    failed = segra_file(settings.recorder_dir / "failed.mp4", 1000, 90)
    _finish(store, failed, "failed")
    old = segra_file(settings.recorder_dir / "old.mp4", 1000, 90)
    _finish(store, old)
    upload = plain_file(settings.data_dir / "inbox" / "bob" / ("e" * 64) / "game.mp4", 1000, 80)
    staged = plain_file(settings.data_dir / "inbox" / ".staging" / "bob" / ("f" * 64) / "game.mp4", 1000, 80)
    foreign = plain_file(settings.output_dir / "notes.mp4", 1000, 80)  # not named like a clip file

    p = plan(settings, store, NOW)
    by = {e.path: e for e in p.managed}
    assert all(not by[k].eligible and by[k].reason.startswith("kept") for k in kept)
    assert all(by[d].eligible for d in dropped)
    assert by[recent].reason.startswith("recent")
    assert by[fresh_unprocessed].reason.startswith("unprocessed")
    assert by[failed].reason.startswith("unprocessed")
    assert by[old].eligible and by[upload].reason.startswith("unprocessed")
    assert staged not in by and foreign not in by
    taken = {e.path for e in p.delete_order}
    assert not taken & set(kept) and recent not in taken and fresh_unprocessed not in taken
    assert old in taken and all(f.exists() for f in kept + dropped)


def test_upload_eligible_once_ingested(settings: Settings, store: Store) -> None:
    up = plain_file(settings.data_dir / "inbox" / "bob" / ("e" * 64) / "game.mp4", 3000, 80)
    _finish(store, up)
    p = plan(settings, store, NOW)
    assert [e.path for e in p.delete_order] == [up]
    assert p.delete_order[0].source == "upload" and "already processed" in p.delete_order[0].reason


def test_shortfall_is_reported(settings: Settings, store: Store) -> None:
    segra_file(settings.recorder_dir / "recent.mp4", 5000, 1)
    _finish(store, settings.recorder_dir / "recent.mp4")
    d = plan(settings, store, NOW).to_dict()
    assert d["reaches_cap"] is False and d["plan"] == [] and d["protected_files"] == 1


def test_module_has_no_delete_code() -> None:
    src = Path(retention.__file__).read_text()
    assert not re.search(r"unlink|remove|rmtree|rmdir|trash|shutil|\.rename|\.replace\(|os\.replace", src)
    assert "decision for Jay" in src


def test_cli_retention(settings: Settings, tmp_path: Path, monkeypatch, capsys) -> None:
    old = segra_file(settings.recorder_dir / "a.mp4", 3000, 60)
    monkeypatch.setattr("autoclipper.config.load_dotenv", lambda *a, **k: None)
    for key, val in {
        "AUTOCLIPPER_REPLAY_DIR": settings.replay_dir, "AUTOCLIPPER_OUTPUT_DIR": settings.output_dir,
        "AUTOCLIPPER_DATA_DIR": settings.data_dir, "AUTOCLIPPER_RECORDER_DIR": settings.recorder_dir,
    }.items():  # fmt: skip
        monkeypatch.setenv(key, str(val))
    monkeypatch.setenv("AUTOCLIPPER_RETENTION_GB", str(2000 / GB))
    with Store(settings) as s:
        _finish(s, old)
    # mtime was set relative to NOW (2027); that is "future" to the real clock, so age it for the real one
    os.utime(old, (time.time() - 60 * DAY,) * 2)
    assert main(["retention", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["report_only"] is True and [e["path"] for e in data["plan"]] == [str(old)]
    assert main(["retention"]) == 0
    out = capsys.readouterr().out
    assert "REPORT ONLY" in out and str(old) in out and old.exists()
