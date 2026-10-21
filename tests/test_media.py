from __future__ import annotations

import json
import shutil
import subprocess
import time
import wave
from pathlib import Path

import pytest

from autoclipper import media

VIDEOS = Path("/mnt/c/Users/jayvi/Videos")
REAL_BLACK = [VIDEOS / "Replay 2026-10-20 13-50-58.mp4", VIDEOS / "Replay 2026-10-20 09-01-16.mp4"]


def test_probe_fields(fixtures_dir: Path) -> None:
    info = media.probe(fixtures_dir / "four_tracks.mp4")
    assert (info.width, info.height) == (640, 360)
    assert info.fps == pytest.approx(30, abs=0.1)
    assert info.video_codec == "h264"
    assert info.duration == pytest.approx(10, abs=0.3)
    assert info.size_bytes > 0 and info.bitrate > 0
    assert [a.order for a in info.audio] == [0, 1, 2, 3]
    assert all(a.codec == "aac" and a.sample_rate > 0 and a.channels >= 1 for a in info.audio)
    assert [a.index for a in media.audio_tracks(fixtures_dir / "four_tracks.mp4")] == [
        a.index for a in info.audio
    ]


def test_keyframe_times(fixtures_dir: Path) -> None:
    keys = media.keyframe_times(fixtures_dir / "firefight.mp4")
    assert keys[0] == pytest.approx(0, abs=0.05)
    assert keys == sorted(keys)
    assert 15 <= len(keys) <= 25  # ~1 s keyframes over 20 s


@pytest.mark.parametrize(
    ("name", "dead"), [("black.mp4", True), ("static.mp4", True), ("firefight.mp4", False)]
)
def test_is_dead_capture(fixtures_dir: Path, name: str, dead: bool) -> None:
    verdict, reason = media.is_dead_capture(fixtures_dir / name)
    assert verdict is dead, reason
    assert reason


def test_detect_intervals(fixtures_dir: Path) -> None:
    black = media.detect_black(fixtures_dir / "black.mp4")
    assert black and black[0].duration > 0.8 * media.probe(fixtures_dir / "black.mp4").duration
    assert media.detect_black(fixtures_dir / "firefight.mp4") == []
    assert media.detect_frozen(fixtures_dir / "static.mp4")
    assert media.detect_frozen(fixtures_dir / "firefight.mp4") == []


def test_extract_audio_four_tracks(fixtures_dir: Path, tmp_path: Path) -> None:
    src = fixtures_dir / "four_tracks.mp4"
    for track in media.audio_tracks(src):
        out = media.extract_audio(src, track.index, tmp_path / f"t{track.order}.wav")
        with wave.open(str(out)) as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
            assert w.getnframes() / 16000 == pytest.approx(10, abs=0.3)
    mix = media.extract_audio(src, None, tmp_path / "mix.wav")
    assert mix.stat().st_size > 1000
    with pytest.raises(media.MediaError):
        media.extract_audio(src, 0, tmp_path / "video.wav")  # stream 0 is video


def test_cut_copy_starts_on_keyframe(fixtures_dir: Path, tmp_path: Path) -> None:
    src = fixtures_dir / "firefight.mp4"
    res = media.cut_copy(src, 5.5, 10.5, tmp_path / "cut.mp4")
    assert res.start in media.keyframe_times(src)
    assert res.start <= 5.5 + 1e-6 and 5.5 - res.start < 1.5
    assert res.end == 10.5
    info = media.probe(res.path)
    assert info.duration == pytest.approx(10.5 - 5.5, abs=1.0) or info.duration == pytest.approx(
        res.end - res.start, abs=1.0
    )
    assert len(info.audio) == 1
    assert media.keyframe_times(res.path)[0] == pytest.approx(0, abs=0.1)


def test_cut_copy_keeps_all_audio_tracks(fixtures_dir: Path, tmp_path: Path) -> None:
    res = media.cut_copy(fixtures_dir / "four_tracks.mp4", 2, 6, tmp_path / "c4.mp4")
    assert len(media.probe(res.path).audio) == 4


@pytest.mark.parametrize("max_mb", [19.0, 0.4])
def test_share_encode_fits(fixtures_dir: Path, tmp_path: Path, max_mb: float) -> None:
    res = media.share_encode(
        fixtures_dir / "firefight.mp4", 2, 14, tmp_path / "s.mp4", max_mb=max_mb, encoder="libx264"
    )
    assert res.size_bytes <= max_mb * 1_000_000
    assert res.path.stat().st_size == res.size_bytes
    info = media.probe(res.path)
    assert info.video_codec == "h264" and info.height <= 1080 and info.fps <= 60.5
    assert info.audio and info.audio[0].codec == "aac"
    assert info.duration == pytest.approx(12, abs=0.5)
    first = subprocess.run(["head", "-c", "64", str(res.path)], capture_output=True).stdout
    assert b"ftyp" in first


def test_nvenc_cached_and_usable(fixtures_dir: Path, tmp_path: Path) -> None:
    games = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         "Get-Process EscapeFromTarkov,WardogsClient-Win64-Shipping,cs2 -ErrorAction SilentlyContinue"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    ).stdout.strip()  # fmt: skip
    if games:
        pytest.skip("a game is running; not touching the GPU")
    if not media.nvenc_works():
        pytest.skip("h264_nvenc not usable here")
    res = media.share_encode(fixtures_dir / "firefight.mp4", 0, 5, tmp_path / "n.mp4", encoder="h264_nvenc")
    assert res.encoder == "h264_nvenc" and res.size_bytes <= 19_000_000


def test_never_overwrites_input(fixtures_dir: Path, tmp_path: Path) -> None:
    src = tmp_path / "in.mp4"
    shutil.copy(fixtures_dir / "firefight.mp4", src)
    before = src.read_bytes()
    link = tmp_path / "link.mp4"
    link.symlink_to(src)
    for out in (src, link):
        with pytest.raises(media.MediaError):
            media.cut_copy(src, 0, 2, out)
        with pytest.raises(media.MediaError):
            media.share_encode(src, 0, 2, out)
        with pytest.raises(media.MediaError):
            media.thumbnail(src, 1, out)
    assert src.read_bytes() == before


def test_thumbnail_and_webp(fixtures_dir: Path, tmp_path: Path) -> None:
    jpg = media.thumbnail(fixtures_dir / "firefight.mp4", 3.0, tmp_path / "t.jpg", width=320)
    data = jpg.read_bytes()
    assert data[:3] == b"\xff\xd8\xff"
    info = media.probe(jpg)
    assert info.width == 320
    webp = media.preview_webp(fixtures_dir / "firefight.mp4", 5.0, tmp_path / "p.webp", seconds=2)
    raw = webp.read_bytes()
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" and b"ANIM" in raw[:64]
    assert len(raw) < 2_000_000


@pytest.mark.real_media
def test_real_black_replays_flagged_and_normal_not() -> None:
    missing = [p for p in REAL_BLACK if not p.exists()]
    if missing:
        pytest.skip(f"real replays absent: {missing}")
    for p in REAL_BLACK:
        t0 = time.perf_counter()
        dead, reason = media.is_dead_capture(p)
        elapsed = time.perf_counter() - t0
        print(f"{p.name}: {dead} {reason} in {elapsed:.2f}s")
        assert dead, reason
        assert elapsed < 10
    candidates = sorted(VIDEOS.glob("Replay*.mp4"), key=lambda p: p.stat().st_size, reverse=True)
    if not candidates:
        pytest.skip("no normal replay available")
    normal = candidates[0]
    t0 = time.perf_counter()
    dead, reason = media.is_dead_capture(normal)
    elapsed = time.perf_counter() - t0
    print(f"{normal.name}: {dead} {reason} in {elapsed:.2f}s")
    assert not dead, reason
    assert elapsed < 10 * max(1.0, media.probe(normal).duration / 60)


@pytest.mark.real_media
def test_real_replay_probe_and_keyframes() -> None:
    p = REAL_BLACK[0]
    if not p.exists():
        pytest.skip("real replay absent")
    info = media.probe(p)
    assert (info.width, info.height) == (2560, 1440) and info.fps == pytest.approx(60, abs=0.5)
    assert media.keyframe_times(p)[0] == pytest.approx(0, abs=0.05)
    json.dumps(info.size_bytes)
