from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

DURATIONS = {"black": 10, "static": 10, "firefight": 20, "speech": 8, "four_tracks": 10}


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    return json.loads(out.stdout)


def ffmpeg_stderr(*args: str) -> str:
    return subprocess.run(["ffmpeg", "-hide_banner", *args], capture_output=True, text=True).stderr


@pytest.mark.parametrize("name", DURATIONS)
def test_media_shape(fixtures_dir: Path, name: str) -> None:
    info = probe(fixtures_dir / f"{name}.mp4")
    kinds = [s["codec_type"] for s in info["streams"]]
    assert kinds.count("video") == 1
    assert kinds.count("audio") == (4 if name == "four_tracks" else 1)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["codec_name"], video["width"], video["height"]) == ("h264", 640, 360)
    assert video["r_frame_rate"] == "30/1"
    assert float(info["format"]["duration"]) == pytest.approx(DURATIONS[name], abs=0.15)


@pytest.mark.parametrize("name", DURATIONS)
def test_one_second_keyframes(fixtures_dir: Path, name: str) -> None:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-skip_frame", "nokey",
         "-show_entries", "frame=pts_time", "-of", "default=nw=1:nk=1", str(fixtures_dir / f"{name}.mp4")],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    times = [float(t) for t in out.stdout.split()]
    assert times == pytest.approx([float(i) for i in range(len(times))], abs=0.05)
    assert len(times) >= DURATIONS[name] - 1


@pytest.mark.parametrize("name", ["firefight", "speech", "four_tracks"])
def test_ground_truth_json(fixtures_dir: Path, name: str) -> None:
    truth = json.loads((fixtures_dir / f"{name}.json").read_text())
    assert truth["duration_s"] == DURATIONS[name]
    if name == "speech":
        assert truth["phrases"][0]["text"] == "clip that"
        assert truth["phrases"][0]["start_s"] == 3.0
    else:
        assert truth["burst_times_s"]


def test_four_tracks_titles(fixtures_dir: Path) -> None:
    info = probe(fixtures_dir / "four_tracks.mp4")
    titles = [s["tags"]["handler_name"] for s in info["streams"] if s["codec_type"] == "audio"]
    assert titles == ["mix", "game", "discord", "mic"]


def test_black_is_flagged(fixtures_dir: Path) -> None:
    err = ffmpeg_stderr(
        "-i", str(fixtures_dir / "black.mp4"), "-vf", "blackdetect=d=2", "-an", "-f", "null", "-"
    )
    assert re.search(r"black_start:0", err)


def test_static_is_frozen_not_black(fixtures_dir: Path) -> None:
    path = str(fixtures_dir / "static.mp4")
    assert "freeze_start" in ffmpeg_stderr(
        "-i", path, "-vf", "freezedetect=n=-50dB:d=2", "-an", "-f", "null", "-"
    )
    assert "black_start" not in ffmpeg_stderr("-i", path, "-vf", "blackdetect=d=2", "-an", "-f", "null", "-")


def test_firefight_moves(fixtures_dir: Path) -> None:
    err = ffmpeg_stderr(
        "-i", str(fixtures_dir / "firefight.mp4"), "-vf", "freezedetect=n=-50dB:d=2", "-an", "-f", "null", "-"
    )
    assert "freeze_start" not in err


def test_firefight_bursts_are_loud(fixtures_dir: Path) -> None:
    truth = json.loads((fixtures_dir / "firefight.json").read_text())
    path = str(fixtures_dir / "firefight.mp4")

    def peak(start: float, dur: float) -> float:
        err = ffmpeg_stderr(
            "-ss", str(start), "-t", str(dur), "-i", path, "-vn", "-af", "volumedetect", "-f", "null", "-"
        )
        return float(re.search(r"max_volume: (-?[\d.]+) dB", err).group(1))  # type: ignore[union-attr]

    assert peak(truth["burst_times_s"][0] - 0.05, 0.2) > -10
    assert peak(9.0, 1.0) < -25  # quiet bed between bursts


def test_eft_excerpts_layout(eft_logs_dir: Path) -> None:
    dirs = sorted(d for d in eft_logs_dir.iterdir() if d.is_dir() and d.name.startswith("log_"))
    assert len(dirs) >= 2
    for d in dirs:
        logs = list(d.glob("* application_000.log"))
        assert len(logs) == 1 and logs[0].name.startswith(d.name[4:])
    text = "\n".join(p.read_text() for p in eft_logs_dir.glob("log_*/*.log"))
    for marker in ("GameStarting:", "GameStarted:", "Location: ", "Matching with group id:",
                   "Network game matching cancelled.", "PrepareSelectedProfileLocally"):  # fmt: skip
        assert marker in text
    assert not re.search(r"(?<![0-9a-fA-F])[0-9a-fA-F]{24}(?![0-9a-fA-F])", text)
    ips = set(re.findall(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])", text))
    assert all(ip.startswith("203.0.113.") for ip in ips)
