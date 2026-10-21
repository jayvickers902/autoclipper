from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest

from autoclipper import signals
from autoclipper.signals import keywords, loudness

VIDEOS = Path("/mnt/c/Users/jayvi/Videos")
WHISPER_TEST_MODEL = "base.en"


def _truth(fixtures_dir: Path, name: str) -> dict:
    return json.loads((fixtures_dir / f"{name}.json").read_text())


@pytest.fixture(scope="module")
def firefight(fixtures_dir: Path) -> dict:
    return signals.analyze(fixtures_dir / "firefight.mp4", whisper=False)


def test_transients_find_every_burst(fixtures_dir: Path, firefight: dict) -> None:
    truth = _truth(fixtures_dir, "firefight")["burst_times_s"]
    found = [e["t"] for e in firefight["events"] if e["kind"] == "transient"]
    assert len(found) == len(truth), found
    for want, got in zip(truth, found, strict=True):
        assert got == pytest.approx(want, abs=0.1)
    assert all(0 < e["strength"] <= 1 and e["role"] == "mix" for e in firefight["events"])


def test_per_second_levels_and_onsets(firefight: dict) -> None:
    assert firefight["version"] == 2 and not firefight["dead"]
    secs = firefight["seconds"]["mix"]
    assert [s["t"] for s in secs] == list(range(20))
    keys = {"t", "rms_db", "peak_db", "onsets", "speech", "tags"}
    assert all(set(s) == keys and s["tags"] == {} for s in secs)
    assert sum(s["onsets"] for s in secs) == 6
    assert {i for i, s in enumerate(secs) if s["onsets"]} <= {6, 7, 11, 12}  # 12.0 s may land at 11.99
    assert secs[6]["peak_db"] > -20 > secs[3]["peak_db"] + 10  # burst second is far louder than the bed
    assert secs[6]["rms_db"] > secs[3]["rms_db"]
    assert not any(s["speech"] for s in secs)


def test_no_transients_in_speech_or_sine(fixtures_dir: Path) -> None:
    sp = signals.analyze(fixtures_dir / "speech.mp4", whisper=False)
    assert [e for e in sp["events"] if e["kind"] == "transient"] == []
    ft = signals.analyze(fixtures_dir / "four_tracks.mp4", whisper=False)
    assert [e for e in ft["seconds"]["mic"] if e["onsets"]] == []  # steady 440 Hz sine


def test_transients_synthetic_edge_cases() -> None:
    sr = 16_000
    assert loudness.detect_transients(np.zeros(sr, dtype=np.float32), sr) == []
    assert loudness.detect_transients(np.zeros(100, dtype=np.float32), sr) == []
    x = np.random.default_rng(0).uniform(-0.005, 0.005, 3 * sr).astype(np.float32)
    burst = 0.8 * np.exp(-np.arange(800) / 150) * np.random.default_rng(1).uniform(-1, 1, 800)
    x[sr : sr + 800] += burst.astype(np.float32)
    hits = loudness.detect_transients(x, sr)
    assert [round(t, 1) for t, _ in hits] == [1.0]


def test_four_track_roles_and_sources(fixtures_dir: Path) -> None:
    r = signals.analyze(fixtures_dir / "four_tracks.mp4", whisper=False)
    assert [t["role"] for t in r["tracks"]] == ["mix", "game", "discord", "mic"]
    assert r["sources"]["transients"] == "game" and r["sources"]["speech"] == []
    assert set(r["seconds"]) == {"mix", "game", "discord", "mic"}
    truth = _truth(fixtures_dir, "four_tracks")["burst_times_s"]
    found = [e for e in r["events"] if e["kind"] == "transient"]
    assert [e["role"] for e in found] == ["game"] * len(truth)
    for want, e in zip(truth, found, strict=True):
        assert e["t"] == pytest.approx(want, abs=0.1)


def test_roles_for_other_track_counts() -> None:
    from autoclipper.signals.analyze import assign_roles

    assert assign_roles(1) == ["mix"] and assign_roles(0) == []
    assert assign_roles(2) == ["mix", "track_2"] and assign_roles(3) == ["mix", "track_2", "track_3"]


def test_dead_capture_skips_audio(fixtures_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "sub" / "black.json"
    r = signals.analyze(fixtures_dir / "black.mp4", out)
    assert r["dead"] and r["dead_reason"] and r["seconds"] == {}
    assert r["events"] == [] and r["transcript"] == []
    assert json.loads(out.read_text()) == r
    assert signals.analyze(fixtures_dir / "static.mp4")["dead"]


def test_tagger_hook(fixtures_dir: Path) -> None:
    calls: list[str] = []

    def tagger(samples: np.ndarray, sr: int, role: str) -> list[dict]:
        calls.append(role)
        return [{"Gunshot": 0.9}] + [{}] * 19

    r = signals.analyze(fixtures_dir / "firefight.mp4", whisper=False, tagger=tagger)
    assert calls == ["mix"] and r["seconds"]["mix"][0]["tags"] == {"Gunshot": 0.9}
    assert r["tagger"] == "custom" and r["whisper_device"] == ""


def test_default_has_no_tagger_and_v2_fields(firefight: dict) -> None:
    assert (firefight["tagger"], firefight["whisper_device"], firefight["whisper_model"]) == ("", "", "")


def test_tagger_windows_and_groups() -> None:
    from autoclipper.signals import tagger as tg

    x = np.arange(int(2.5 * tg.SR), dtype=np.float32)
    ((first, batch),) = list(tg.window_batches(x, 3))
    assert first == 0 and batch.shape == (3, 2 * tg.SR)
    assert batch[1, tg.SR // 2] == x[tg.SR]  # second 1's window starts 0.5 s before it
    assert batch[0, 0] == 0.0 and batch[2, -1] == 0.0  # zero padded at the edges
    long = np.zeros(40 * tg.SR, np.float32)
    assert [b.shape[0] for _, b in tg.window_batches(long, 40, batch=16)] == [16, 16, 8]


def test_tagger_rejects_other_sample_rates() -> None:
    from autoclipper.signals.tagger import PannsTagger

    with pytest.raises(ValueError, match="16000"):
        PannsTagger()(np.zeros(8000, np.float32), 8000, "mix")


def test_tagger_downloads_are_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import contextlib
    import hashlib

    import httpx

    from autoclipper.signals import tagger as tg

    fetched: list[str] = []

    class FakeResponse:
        def raise_for_status(self) -> None: ...

        def iter_bytes(self, _n: int):
            yield b"hel"
            yield b"lo"

    @contextlib.contextmanager
    def fake_stream(_method: str, url: str, **_kw):
        fetched.append(url)
        yield FakeResponse()

    monkeypatch.setattr(httpx, "stream", fake_stream)
    good = hashlib.sha256(b"hello").hexdigest()
    monkeypatch.setattr(tg, "FILES", {"bad.bin": ("http://x/bad.bin", "0" * 64)})
    with pytest.raises(RuntimeError, match="sha256"):
        tg.ensure_files(tmp_path)
    assert not (tmp_path / "bad.bin").exists() and not list(tmp_path.glob("*.part"))
    monkeypatch.setattr(tg, "FILES", {"ok.bin": ("http://x/ok.bin", good)})
    assert tg.ensure_files(tmp_path) == tmp_path and (tmp_path / "ok.bin").read_bytes() == b"hello"
    tg.ensure_files(tmp_path)  # cached and verified: no second download
    assert fetched == ["http://x/bad.bin", "http://x/ok.bin"]


@pytest.mark.parametrize(
    "text, want",
    [
        ("Clip that!", ["clip that"]),
        ("clipp that", ["clip that"]),
        ("Nice shot.", ["nice shot"]),  # beats "nice"
        ("nice one", ["nice"]),
        ("Let’s go, lets go", ["let's go", "let's go"]),
        ("Oh my God, no way", ["oh my god", "no way"]),
        ("hes down", ["he's down"]),
        ("the clip is far that", []),
        ("what a night", []),
    ],
)
def test_keyword_matching(text: str, want: list[str]) -> None:
    words = [{"start": i * 0.3, "end": i * 0.3 + 0.25, "word": w} for i, w in enumerate(text.split())]
    assert [k["phrase"] for k in keywords.find_keywords(words)] == want


def test_keyword_times() -> None:
    words = [{"start": 5.0, "end": 5.2, "word": "Nice"}, {"start": 5.3, "end": 5.7, "word": "shot."}]
    (k,) = keywords.find_keywords(words)
    assert (k["t"], k["end"], k["text"]) == (5.0, 5.7, "Nice shot.")


@pytest.mark.whisper
def test_speech_keywords(fixtures_dir: Path) -> None:
    r = signals.analyze(
        fixtures_dir / "speech.mp4", whisper_model=WHISPER_TEST_MODEL, bias_keywords=True, check_dead=False
    )
    assert r["sources"]["speech"] == ["mix"] and r["whisper_model"] == WHISPER_TEST_MODEL
    kws = {e["phrase"]: e for e in r["events"] if e["kind"] == "keyword"}
    assert kws["clip that"]["t"] == pytest.approx(3.0, abs=0.7)
    assert kws["nice shot"]["t"] == pytest.approx(6.0, abs=0.7)
    assert all(e["role"] == "mix" for e in kws.values())
    assert r["transcript"] and all(w["start"] <= w["end"] for s in r["transcript"] for w in s["words"])
    secs = r["seconds"]["mix"]
    assert secs[3]["speech"] and secs[6]["speech"] and not secs[0]["speech"] and not secs[-1]["speech"]
    assert [e for e in r["events"] if e["kind"] == "transient"] == []


@pytest.mark.whisper
def test_four_tracks_speech_comes_from_discord(fixtures_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "ft.json"
    r = signals.analyze(
        fixtures_dir / "four_tracks.mp4", out, whisper_model=WHISPER_TEST_MODEL, bias_keywords=True
    )
    assert r["sources"] == {"transients": "game", "speech": ["discord", "mic"]}
    kw = [e for e in r["events"] if e["kind"] == "keyword"]
    assert [(e["phrase"], e["role"]) for e in kw] == [("clip that", "discord")]
    assert kw[0]["t"] == pytest.approx(4.0, abs=0.7)
    assert {s["role"] for s in r["transcript"]} == {"discord"}
    assert json.loads(out.read_text()) == r


@pytest.mark.real_media
@pytest.mark.whisper
def test_real_replay_timing() -> None:
    src = VIDEOS / "Replay 2026-10-19 23-34-12.mp4"
    if not src.exists():
        pytest.skip("real replay not present")
    t0 = time.perf_counter()
    r = signals.analyze(src, whisper_model=WHISPER_TEST_MODEL)
    assert not r["dead"] and r["seconds"]
    assert time.perf_counter() - t0 < 120


def _real_tagger():
    from autoclipper.signals.tagger import PannsTagger

    return PannsTagger()  # first use downloads the 328 MB model to ~/.cache/autoclipper


@pytest.mark.real_media
def test_real_tagger_on_fixtures(fixtures_dir: Path) -> None:
    tagger = _real_tagger()
    r = signals.analyze(fixtures_dir / "speech.mp4", whisper=False, tagger=tagger, check_dead=False)
    assert r["tagger"] == "panns-cnn14-16k"
    secs = r["seconds"]["mix"]
    assert secs[3]["tags"].get("speech", 0) > 0.3  # "clip that" at 3 s is synthetic speech
    assert secs[3]["tags"].get("speech", 0) > secs[0]["tags"].get("speech", 0)
    assert all(0 <= v <= 1 for s in secs for v in s["tags"].values())
    # a muted track gets no tags at all, and the batch path agrees with one-second-at-a-time
    assert tagger(np.zeros(5 * 16_000, np.float32), 16_000, "mic") == [{}] * 5
    x, sr = loudness.read_wav(_wav_of(fixtures_dir / "speech.mp4"))
    assert tagger(x[: 4 * sr], sr, "mix")[:3] == tagger(x, sr, "mix")[:3]


def _wav_of(video: Path) -> Path:
    import subprocess
    import tempfile

    out = Path(tempfile.mkdtemp(prefix="autoclipper-tagtest-")) / "a.wav"
    cmd = ["ffmpeg", "-v", "error", "-i", str(video), "-ac", "1", "-ar", "16000", str(out)]
    subprocess.run(cmd, check=True)
    return out


@pytest.mark.real_media
def test_real_tagger_on_replay() -> None:
    src = VIDEOS / "Replay 2026-10-19 23-34-12.mp4"
    if not src.exists():
        pytest.skip("real replay not present")
    tagger = _real_tagger()
    t0 = time.perf_counter()
    r = signals.analyze(src, whisper=False, tagger=tagger)
    wall = time.perf_counter() - t0
    assert not r["dead"] and r["tagger"] == "panns-cnn14-16k"
    n_tracks = len(r["seconds"])
    assert wall < 10 * max(1, n_tracks)  # target: < 10 s per 60 s replay and track
    tags = [t for entries in r["seconds"].values() for e in entries for t in e["tags"]]
    assert tags and set(tags) <= {"gunshot", "explosion", "laughter", "shout", "speech", "music"}
