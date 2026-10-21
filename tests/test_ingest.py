"""Long-recording ingestion (P4.1): EFT raid segmentation, hints, chunking, split groups, store and CLI.

The media tests build their recordings at test time (fixtures/make_fixtures.py `make_long`): 160x90 testsrc2,
one 16 kHz track with gunshot-like bursts, optional chapters. Fake judge only; whisper off; libx264 only.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import struct
from datetime import datetime
from pathlib import Path

import pytest

from autoclipper import eft, hints
from autoclipper.candidates import find_candidates
from autoclipper.cli import main
from autoclipper.config import Settings
from autoclipper.ingest import (
    IngestOptions,
    Rec,
    chunk_plan,
    group_splits,
    ingest,
    started_at,
    stitch,
)  # fmt: skip
from autoclipper.judging import FakeJudge
from autoclipper.pipeline import Context, Options, ReplayResult
from autoclipper.store import Store

FIXTURES = Path(__file__).resolve().parent / "fixtures"
T = datetime.fromisoformat

# The 10-minute recording starts 20:30:00; the EFT fixture's raid on Streets runs 20:32:46-20:42:49.
LONG_NAME = "2026-10-12 20-30-00.mp4"
LONG_BURSTS = [95, 96.2, 97.5, 99, 236, 237.5, 239, 241, 242.5, 455, 530, 531, 532.5, 534]
LONG_CHAPTERS = [(0.0, "Start"), (460.0, "Chapter 1")]


def _make_fixtures():
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is needed")
    spec = importlib.util.spec_from_file_location("make_fixtures", FIXTURES / "make_fixtures.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def long_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("long")
    _make_fixtures().make_long(out / LONG_NAME, 600, LONG_BURSTS, LONG_CHAPTERS)
    return out


def _settings(tmp_path: Path, replay_dir: Path) -> Settings:
    return Settings(replay_dir=replay_dir, output_dir=tmp_path / "out", data_dir=tmp_path / "data")


def _ctx(settings: Settings) -> Context:
    return Context(settings, Store(settings), FakeJudge(), Options(whisper=False, encoder="libx264"))


def _hashes(root: Path) -> dict[str, str]:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*.mp4"))}


# --- EFT logs ---------------------------------------------------------------------------------------------


def test_read_raids_from_fixtures(eft_logs_dir: Path) -> None:
    raids = eft.read_raids(eft_logs_dir)
    assert [(r.map_id, r.map_name, r.start, r.end, r.interrupted) for r in raids] == [
        ("TarkovStreets", "Streets of Tarkov", T("2026-10-12 20:32:46"), T("2026-10-12 20:42:49.906"), False),
        (
            "TarkovStreets",
            "Streets of Tarkov",
            T("2026-10-12 21:37:08.828"),
            T("2026-10-12 21:42:30.386"),
            False,
        ),
    ]  # the aborted first matching and the cancelled third one are not raids
    # log dirs are pruned by name: the 20-22-21 launch ended before 21:33 (the next one started 21:32:05)
    assert [r.start.hour for r in eft.read_raids(eft_logs_dir, since=T("2026-10-12 21:33"))] == [21]
    assert eft.read_raids(eft_logs_dir, until=T("2026-10-12 19:00")) == []


def _line(t: str, msg: str) -> str:
    return f"{t}|1.2.0.0.47965|Info|application|{msg}\n"


LOC = "TRACE-NetworkGameCreate profileStatus: 'Profileid: X, Status: Busy, Location: bigmap, Sid: s'"


def test_parser_rules_mirror_recorder() -> None:
    p = eft.LogParser()
    assert p.parse("﻿" + _line("2026-10-13 10:00:00.000", LOC)).map_id == "bigmap"
    p.parse(_line("2026-10-13 10:00:01.000", "Network game matching cancelled."))
    ev = p.parse(_line("2026-10-13 10:00:02.000", "GameStarted:1(1) real:1(1) diff:0"))
    assert ev.kind == "raid_start" and ev.map_id is None  # the cancel forgot the map
    assert p.parse(_line("2026-10-13 10:00:03.000", LOC)) is None  # no map change mid-raid
    assert p.parse(_line("2026-10-13 10:00:03.500", "GameStarted:1(1) real:1(1) diff:0")) is None
    end = p.parse(_line("2026-10-13 10:05:00.000", "PrepareSelectedProfileLocally ProfileId:X"))
    assert end.kind == "raid_end" and not end.interrupted
    assert p.parse(_line("2026-10-13 10:05:01.000", "PrepareSelectedProfileLocally ProfileId:X")) is None
    assert p.parse("2026-10-13 10:05:02.000|v|Info|network|GameStarted:1") is None  # other channel
    assert eft.map_name("RezervBase") == "Reserve" and eft.map_name("NewMap") == "NewMap"


def _write_log(root: Path, stamp: str, lines: list[str]) -> None:
    d = root / f"log_{stamp}_1.2.0.0.1"
    d.mkdir(parents=True)
    (d / f"{stamp}_1.2.0.0.1 application_000.log").write_text("".join(lines), encoding="utf-8")


def test_interrupted_raid(tmp_path: Path) -> None:
    raid = [
        _line("2026-10-13 10:00:00.000", LOC),
        _line("2026-10-13 10:01:00.000", "GameStarted:1(1) real:1(1) diff:0"),
        _line("2026-10-13 10:09:00.000", "something else"),
    ]
    _write_log(tmp_path, "2026.10.13_09-59-00", raid)
    (r,) = eft.read_raids(tmp_path)  # latest launch: still open, clipped to the recording's end
    assert r.end is None and r.interrupted and r.map_name == "Customs"
    (seg,) = eft.segments([r], T("2026-10-13 10:00:00"), 300.0)
    assert (seg.start, seg.end, seg.interrupted, seg.label) == (60.0, 300.0, True, "Customs")
    _write_log(tmp_path, "2026.10.13_11-00-00", [_line("2026-10-13 11:00:00.000", "Application awaken")])
    (r,) = eft.read_raids(tmp_path)  # the game was restarted: the raid ended with its log
    assert r.end == T("2026-10-13 10:09:00") and r.interrupted


def test_segments_clip_to_recording(eft_logs_dir: Path) -> None:
    raids = eft.read_raids(eft_logs_dir)
    (seg,) = eft.segments(raids, T("2026-10-12 20:30:00"), 600.0)
    assert (seg.start, seg.end, seg.map_id) == (166.0, 600.0, "TarkovStreets")
    assert seg.raid_start == "2026-10-12 20:32:46" and not seg.interrupted
    assert eft.segments(raids, T("2026-10-12 23:00:00"), 600.0) == []
    assert eft.segment_at([seg], 200.0) is seg and eft.segment_at([seg], 100.0) is None


# --- hints and start times --------------------------------------------------------------------------------


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def segra_box(meta: dict) -> bytes:
    return _box(b"uuid", hints.SEGRA_BOX_ID + json.dumps(meta).encode())


def embed_segra(path: Path, meta: dict) -> None:
    """Add Segra's metadata box to moov/udta, as the recorder does (moov is last, so offsets stay valid)."""
    data = path.read_bytes()
    pos = 0
    while pos < len(data):
        size, kind = struct.unpack(">I4s", data[pos : pos + 8])
        if kind == b"moov":
            break
        pos += size
    assert pos + size == len(data), "moov must be the last box"
    moov, children, cpos = data[pos + 8 : pos + size], b"", 0
    found = False
    while cpos < len(moov):
        csize, ckind = struct.unpack(">I4s", moov[cpos : cpos + 8])
        child = moov[cpos : cpos + csize]
        if ckind == b"udta":
            child, found = _box(b"udta", child[8:] + segra_box(meta)), True
        children += child
        cpos += csize
    if not found:
        children += _box(b"udta", segra_box(meta))
    path.write_bytes(data[:pos] + _box(b"moov", children))


SEGRA_META = {
    "Game": "Escape From Tarkov", "CreatedAt": "2026-10-12T21:39:00.1234567+02:00",
    "Bookmarks": [
        {"Id": 1, "Type": "Manual", "Subtype": None, "Time": "00:04:08.8280000"},  # recorder's raid start
        {"Id": 2, "Type": "Kill", "Subtype": "Headshot", "Time": "00:05:00"},
    ],
}  # fmt: skip


def test_segra_metadata_and_timespans(tmp_path: Path) -> None:
    f = tmp_path / "fake.mp4"
    moov = _box(b"moov", _box(b"mvhd", b"\0" * 20) + _box(b"udta", segra_box(SEGRA_META)))
    f.write_bytes(_box(b"ftyp", b"isom\0\0\0\0") + _box(b"mdat", b"\0" * 64) + moov)
    assert hints.segra_metadata(f) == SEGRA_META
    f.write_bytes(_box(b"ftyp", b"isom") + _box(b"moov", _box(b"udta", _box(b"uuid", b"x" * 20))))
    assert hints.segra_metadata(f) is None
    assert hints.parse_timespan("00:04:08.8280000") == pytest.approx(248.828)
    assert hints.parse_timespan("1.02:00:00") == 93600.0
    assert hints.parse_timespan("bogus") is None


def test_started_at(tmp_path: Path) -> None:
    obs, segra = tmp_path / "2026-10-12 20-30-00.mp4", tmp_path / "2026-10-12_21-33-00.mp4"
    assert started_at(obs, 60.0) == (T("2026-10-12 20:30:00"), "name")
    assert started_at(segra, 60.0) == (T("2026-10-12 21:33:00"), "name")
    other = tmp_path / "session.mp4"
    other.write_bytes(b"x")
    assert started_at(other, 600.0, SEGRA_META) == (T("2026-10-12 21:29:00"), "segra")
    t, src = started_at(other, 10.0)
    assert src == "mtime" and abs((datetime.now() - t).total_seconds() - 10.0) < 60


# --- candidates with hints, chunk stitching, split groups -------------------------------------------------


def _quiet_timeline(duration: int, events: list[dict]) -> dict:
    sec = [{"t": s, "rms_db": -60.0, "peak_db": -50.0, "onsets": 0, "speech": False, "tags": {}}
           for s in range(duration)]  # fmt: skip
    return {"duration": duration, "dead": False, "sources": {"transients": "mix", "speech": []},
            "seconds": {"mix": sec}, "events": events, "transcript": []}  # fmt: skip


def test_hint_forces_a_candidate() -> None:
    hint = {"t": 100.0, "kind": "hint", "phrase": "Kill", "role": "segra", "pre": 8.0, "post": 4.0}
    (c,) = find_candidates(_quiet_timeline(200, [hint]), min_score=99.0)  # forced past min_score
    assert c.reasons == ["hint:Kill"] and c.start <= 92.5 and 103 <= c.end <= 105.5
    fights = [{"t": t, "kind": "transient", "strength": 0.9, "role": "mix"} for t in (20, 21, 22, 160, 161)]
    cands = find_candidates(_quiet_timeline(200, sorted(fights + [hint], key=lambda e: e["t"])), max_n=1)
    assert [r for c in cands for r in c.reasons if r.startswith("hint:")] == ["hint:Kill"]
    assert len(cands) == 1  # the hint takes the only slot
    assert len(find_candidates(_quiet_timeline(200, fights), max_n=1)) == 1


def test_chunk_plan_and_stitch() -> None:
    assert chunk_plan(25.0, 10, 3) == [(0, 10, 0, 13), (10, 20, 7, 23), (20, float("inf"), 17, 25.0)]
    assert len(chunk_plan(302.0, 300, 10)) == 1  # a 2 s tail is folded into the previous chunk
    duration = 25.0
    parts = []
    for own_a, own_b, read_a, read_b in chunk_plan(duration, 10, 3):
        secs = [{"t": s, "rms_db": float(read_a + s)} for s in range(int(read_b - read_a))]
        evs = [{"t": k / 2, "kind": "transient", "strength": 1.0} for k in range(int(2 * (read_b - read_a)))]
        said = [{"start": 4.0, "end": 5.0, "text": "x", "words": [{"start": 4.0, "end": 5.0, "word": "x"}]}]
        parts.append((own_a, own_b, read_a, {"seconds": {"mix": secs}, "events": evs, "transcript": said}))
    tl = stitch(parts, duration, {"duration": duration})
    assert [e["t"] for e in tl["seconds"]["mix"]] == list(range(25))
    assert all(e["rms_db"] == e["t"] for e in tl["seconds"]["mix"])  # each second from its owner, shifted
    assert [e["t"] for e in tl["events"]] == [k / 2 for k in range(50)]  # once each, no overlap duplicates
    assert [s["start"] for s in tl["transcript"]] == [4.0, 11.0, 21.0]  # read_a 0, 7, 17 + 4
    assert tl["transcript"][1]["words"][0]["start"] == 11.0


def _rec(name: str, started: str | None, duration: float, parent: str = "/v") -> Rec:
    p = Path(parent) / name
    return Rec(p, {}, ReplayResult(p, "new"), duration=duration, started=T(started) if started else None)


def test_group_splits() -> None:
    a = _rec("2026-10-12 21-00-00.mp4", "2026-10-12 21:00:00", 150.02)
    b = _rec("2026-10-12 21-02-30.mp4", "2026-10-12 21:02:30", 150.0)
    c = _rec("2026-10-12 21-05-03.mp4", "2026-10-12 21:05:03", 60.0)  # 3 s gap: still one recording
    d = _rec("2026-10-12 22-00-00.mp4", "2026-10-12 22:00:00", 60.0)
    e = _rec("2026-10-12 22-01-00.mp4", "2026-10-12 22:01:00", 60.0, parent="/other")
    f = _rec("x.mp4", None, 60.0)
    groups = group_splits([d, c, b, a, e, f], 5.0)
    assert [[r.path.name for r in g] for g in groups] == [
        [a.path.name, b.path.name, c.path.name],
        [d.path.name],
        [e.path.name],
        ["x.mp4"],
    ]


# --- end to end -------------------------------------------------------------------------------------------


def test_ingest_long_recording(long_dir: Path, tmp_path: Path, eft_logs_dir: Path) -> None:
    settings = _settings(tmp_path, long_dir)
    before = _hashes(long_dir)
    ctx = _ctx(settings)
    opts = IngestOptions(eft_logs=eft_logs_dir, chunk_s=120, overlap_s=10, stable_wait=0)
    with ctx.store:
        (res,) = ingest(ctx, long_dir, opts)
        assert res.status == "done" and res.candidates == 4 and res.clips == 4, res
        row = ctx.store.replay(res.replay_id)
        assert row["kind"] == "long" and row["started_at"] == "2026-10-12 20:30:00"
        assert row["duration"] == pytest.approx(600, abs=0.1)
        cands = sorted(ctx.store.candidates(res.replay_id), key=lambda c: c["start"])
        windows = [(c["start"], c["end"], c["data"]["reasons"]) for c in cands]
        # in absolute seconds of the source: the menu fight, the fight across the 240 s chunk boundary (one
        # window, not two halves), the chapter marker (forced), the last fight; raids labelled by wall clock
        for (s, e, reasons), (lo, hi, must) in zip(windows, [
            (95, 99, ["outside raid"]), (236, 242.5, ["map:Streets of Tarkov"]),
            (459, 460, ["hint:Chapter 1", "map:Streets of Tarkov"]), (530, 534, ["map:Streets of Tarkov"]),
        ], strict=True):  # fmt: skip
            assert s < lo and hi < e and e - s <= 45, (s, e, reasons)
            assert all(m in reasons for m in must), reasons
        assert sum("fight:5 shots" in r for _, _, r in windows) == 1
        (seg,) = ctx.store.segments(res.replay_id)
        assert (seg["start"], seg["end"], seg["label"]) == (166.0, 600.0, "Streets of Tarkov")
        tl = json.loads(Path(row["timeline_path"]).read_text())
        assert len(tl["seconds"]["mix"]) == 600 and tl["chunks"] == {"size": 120, "overlap": 10}
        assert [e["t"] for e in tl["events"] if e["kind"] == "hint"] == [460.0]
        clips = [ctx.store.clip_for(c["id"]) for c in cands]
        assert all(Path(c["share_path"]).is_file() and c["encoder"] == "libx264" for c in clips)
        assert all(Path(c["share_path"]).is_relative_to(settings.output_dir) for c in clips)
        # idempotent: nothing re-runs
        stages = ctx.store.stages(res.replay_id)
        (again,) = ingest(ctx, long_dir, opts)
        assert again.skipped and ctx.store.stages(res.replay_id) == stages
        assert len(ctx.store.candidates(res.replay_id)) == 4
    assert _hashes(long_dir) == before  # originals untouched
    assert len(list((settings.data_dir / "chunks").rglob("*.json"))) == 5


def test_ingest_resumes_from_cached_chunks(long_dir: Path, tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, long_dir)
    ctx = _ctx(settings)
    from autoclipper import ingest as ingest_mod

    calls = []
    real = ingest_mod.media.extract_audio_window

    def flaky(path, start, length, out):
        calls.append(start)
        if len(calls) == 3:
            raise RuntimeError("crash mid-file")
        return real(path, start, length, out)

    monkeypatch.setattr(ingest_mod.media, "extract_audio_window", flaky)
    opts = IngestOptions(chunk_s=120, overlap_s=10, stable_wait=0)
    with ctx.store:
        (res,) = ingest(ctx, long_dir, opts)
        assert res.status == "failed" and "crash mid-file" in res.note
        (res,) = ingest(ctx, long_dir, opts)
        assert res.status == "done" and res.candidates == 4  # no EFT logs: no map labels, same windows
        assert not any(r.startswith("map:") for c in ctx.store.candidates(res.replay_id)
                       for r in c["data"]["reasons"])  # fmt: skip
    assert calls == [0, 110, 230, 230, 350, 470]  # chunks 1-2 came from the cache on the second run


def test_ingest_split_recording_and_segra_session(tmp_path: Path, eft_logs_dir: Path) -> None:
    mf = _make_fixtures()
    rec_dir = tmp_path / "Videos"
    split = rec_dir / "obs"
    split.mkdir(parents=True)
    # one fight across the split at 150 s (OBS split the recording into two 150 s files)
    mf.make_long(split / "2026-10-12 21-00-00.mp4", 150, [145, 146.5, 148])
    mf.make_long(split / "2026-10-12 21-02-30.mp4", 150, [1.0, 2.5, 4.0, 5.5])
    # a Segra session starting 21:33:00: the raid on Streets starts at 248.8 s (21:37:08.828)
    sess = rec_dir / "Full Sessions" / "Escape From Tarkov" / "2026-10-12_21-33-00.mp4"
    sess.parent.mkdir(parents=True)
    marks = [(0.0, "Start"), (248.828, "Bookmark"), (300.0, "Kill (Headshot)")]  # as Segra writes chapters
    mf.make_long(sess, 360, [120, 121, 122.5], marks)
    embed_segra(sess, SEGRA_META)
    assert hints.segra_metadata(sess) == SEGRA_META and hints.chapters(sess)[0] == (0.0, "Start")
    shutil.copy(split / "2026-10-12 21-00-00.mp4", rec_dir / "Replay 2026-10-12 21-00-00.mp4")  # not ingested

    settings = _settings(tmp_path, rec_dir)
    ctx = _ctx(settings)
    with ctx.store:
        opts = IngestOptions(eft_logs=eft_logs_dir, chunk_s=60, overlap_s=5, stable_wait=0)
        results = ingest(ctx, rec_dir, opts)
        by = {r.path.name: r for r in results}
        assert set(by) == {"2026-10-12 21-00-00.mp4", "2026-10-12 21-02-30.mp4", "2026-10-12_21-33-00.mp4"}
        assert all(r.status == "done" for r in results), results
        # the split fight is one candidate, in the file holding its peak, in that file's seconds
        assert by["2026-10-12 21-00-00.mp4"].candidates + by["2026-10-12 21-02-30.mp4"].candidates == 1
        (c,) = [c for name in ("2026-10-12 21-00-00.mp4", "2026-10-12 21-02-30.mp4")
                for c in ctx.store.candidates(by[name].replay_id)]  # fmt: skip
        assert 0.0 <= c["start"] < c["end"] <= 150.1 and "fight:7 shots" in c["data"]["reasons"]
        # Segra: the recorder's raid-start bookmark is not a moment; the kill bookmark is (forced, mapped)
        rid = by["2026-10-12_21-33-00.mp4"].replay_id
        cands = sorted(ctx.store.candidates(rid), key=lambda c: c["start"])
        reasons = [c["data"]["reasons"] for c in cands]
        assert len(cands) == 2 and "outside raid" in reasons[0]
        assert "hint:Kill (Headshot)" in reasons[1] and "map:Streets of Tarkov" in reasons[1]
        assert not any("hint:Bookmark" in r for rs in reasons for r in rs)
        assert cands[1]["start"] <= 294 and cands[1]["end"] >= 302


def test_cli_ingest(long_dir: Path, tmp_path: Path, eft_logs_dir: Path, monkeypatch, capsys) -> None:
    settings = _settings(tmp_path, long_dir)
    monkeypatch.setenv("AUTOCLIPPER_REPLAY_DIR", str(long_dir))
    monkeypatch.setenv("AUTOCLIPPER_OUTPUT_DIR", str(settings.output_dir))
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(settings.data_dir))
    monkeypatch.setattr("autoclipper.media.encode.nvenc_works", lambda *a, **k: False)  # no GPU in tests
    flags = ["--judge", "fake", "--no-whisper", "--stable-wait", "0", "--chunk", "200", "--overlap", "5"]
    assert main(["ingest", str(long_dir), "--eft-logs", str(eft_logs_dir), *flags]) == 0
    out = capsys.readouterr().out
    assert f"{LONG_NAME}: done" in out and "1 replay(s) processed (1 done), 4 clip(s)" in out
    assert main(["ingest", str(long_dir / LONG_NAME), *flags]) == 0
    assert "already done" in capsys.readouterr().out
    assert main(["ingest", str(tmp_path / "missing"), *flags]) == 2
    assert main(["ingest", str(long_dir), "--chunk", "10", *flags[:-4]]) == 2
