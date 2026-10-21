from __future__ import annotations

from pathlib import Path

from autoclipper.gpu_gate import GateError, GpuGate, parse_games

GAMES = """\
# comment
cs2.exe
EscapeFromTarkov.exe   # trailing comment
!EpicOnlineServicesUserHelper.exe
!cs2.exe
@auto off

Wardogs-Win64-Shipping.exe
"""


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def gate(tmp_path: Path, procs, clock=None, log=None, text=GAMES) -> GpuGate:
    f = tmp_path / "games.txt"
    f.write_text(text)
    return GpuGate(f, processes=procs, clock=clock or Clock(), log=log or (lambda _m: None))


def test_parse_games():
    assert parse_games(GAMES) == {"escapefromtarkov", "wardogs-win64-shipping"}


def test_game_running_switches_encoder(tmp_path: Path):
    g = gate(tmp_path, lambda: {"chrome", "escapefromtarkov"})
    assert g.games_running() is True
    assert g.encoder() == "libx264"
    assert g.encoder("h264_nvenc") == "libx264"


def test_no_game_keeps_preferred(tmp_path: Path):
    g = gate(tmp_path, lambda: {"chrome", "epiconlineservicesuserhelper", "cs2"})
    assert g.games_running() is False
    assert g.encoder() == "auto"
    assert g.encoder("h264_nvenc") == "h264_nvenc"


def test_cached_for_30s(tmp_path: Path):
    clock, calls = Clock(), []

    def procs() -> set[str]:
        calls.append(1)
        return {"escapefromtarkov"} if len(calls) == 1 else set()

    g = gate(tmp_path, procs, clock)
    assert g.games_running() is True
    clock.t += 29
    assert g.games_running() is True and len(calls) == 1
    clock.t += 2
    assert g.games_running() is False and len(calls) == 2


def test_failure_assumes_game_and_logs_once(tmp_path: Path):
    logs: list[str] = []
    clock = Clock()

    def boom() -> set[str]:
        raise GateError("no interop")

    g = gate(tmp_path, boom, clock, logs.append)
    assert g.encoder() == "libx264"
    clock.t += 60
    assert g.games_running() is True
    assert len(logs) == 1 and "no interop" in logs[0]


def test_missing_games_file_assumes_game(tmp_path: Path):
    logs: list[str] = []
    g = GpuGate(tmp_path / "nope.txt", processes=lambda: set(), log=logs.append)
    assert g.games_running() is True
    assert len(logs) == 1
