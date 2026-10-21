"""Keep GPU work (NVENC) off the card while Jay is gaming.

"A game is running" means a process named in q's explicit list (`q-games.txt`: one exe per line, `#`
comments, `!name.exe` = never a game). q's own auto-detection is deliberately not copied: it flags Epic
launcher helpers as games. One `powershell.exe Get-Process` call answers it, cached for ~30 s. If that
call fails (no interop under systemd, timeout, list unreadable) we log it once and assume a game IS running,
so the encode takes the CPU path.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

DEFAULT_GAMES_FILE = Path("/mnt/c/Users/jayvi/bin/q-games.txt")
POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
PS_COMMAND = "Get-Process | ForEach-Object { $_.ProcessName }"
CACHE_S = 30.0
TIMEOUT_S = 15.0
CPU_ENCODER = "libx264"


class GateError(RuntimeError):
    """The process list or the games list could not be read."""


def _norm(name: str) -> str:
    name = name.strip().lower()
    return name[:-4] if name.endswith(".exe") else name


def parse_games(text: str) -> set[str]:
    """Game process names (lower case, no .exe) from q-games.txt text; `!` and `@` lines drop out."""
    games: set[str] = set()
    never: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("@"):
            continue
        if line.startswith("!"):
            never.add(_norm(line[1:]))
        else:
            games.add(_norm(line))
    return games - never


def powershell_processes() -> set[str]:
    """Names of the running Windows processes (lower case, no .exe)."""
    try:
        proc = subprocess.run(
            [POWERSHELL, "-NoProfile", "-Command", PS_COMMAND],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT_S, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError(f"powershell.exe failed: {type(exc).__name__}: {exc}") from exc
    names = {_norm(line) for line in proc.stdout.splitlines() if line.strip()}
    if proc.returncode != 0 or not names:
        raise GateError(f"powershell.exe exit {proc.returncode}, {len(names)} processes listed")
    return names


class GpuGate:
    def __init__(
        self,
        games_file: Path | None = None,
        *,
        processes: Callable[[], set[str]] = powershell_processes,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = lambda _msg: None,
        cache_s: float = CACHE_S,
    ) -> None:
        self.games_file = games_file or Path(os.environ.get("AUTOCLIPPER_GAMES_FILE", DEFAULT_GAMES_FILE))
        self._processes = processes
        self._clock = clock
        self._log = log
        self._cache_s = cache_s
        self._lock = threading.Lock()
        self._at: float | None = None
        self._value = True
        self._warned = False

    def games_running(self) -> bool:
        """True if a listed game runs, or if we cannot tell."""
        with self._lock:
            now = self._clock()
            if self._at is None or now - self._at >= self._cache_s:
                self._value = self._check()
                self._at = now
            return self._value

    def _check(self) -> bool:
        try:
            games = parse_games(self.games_file.read_text(errors="replace"))
            if not games:
                raise GateError(f"no games listed in {self.games_file}")
            running = games & self._processes()
        except (OSError, GateError) as exc:
            if not self._warned:
                self._warned = True
                self._log(f"gpu gate: check failed ({exc}); assuming a game is running (CPU encodes)")
            return True
        return bool(running)

    def encoder(self, preferred: str = "auto") -> str:
        """`preferred` while the GPU is free, libx264 (CPU) while a game runs."""
        return CPU_ENCODER if self.games_running() else preferred
