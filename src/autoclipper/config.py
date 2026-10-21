"""Settings from the environment (and `.env`), with the project defaults."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Registered in C:\projects\homepanel\ports.toml (`ports get autoclipper`). Never pick another.
DEFAULT_PORT = 4180
DEFAULT_HOST = "127.0.0.1"


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from `.env` into os.environ without overriding what is already set."""
    path = path or PROJECT_ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    replay_dir: Path = Path("/mnt/c/Users/jayvi/Videos")
    output_dir: Path = Path("/mnt/c/Users/jayvi/Videos/autoclipper")
    data_dir: Path = PROJECT_ROOT / "data"
    judge: str = "claude-cli"
    judge_model: str = "claude-haiku-5-5"
    polish_model: str = "claude-sonnet-5-5"
    judge_max_calls_per_hour: int = 60
    polish_max_calls_per_day: int = 30
    judge_max_5h: float = 0.9
    judge_max_7d: float = 0.6
    # Run caps on Claude calls, counted over every ledger row ever (failed and timed-out calls included).
    run_max_haiku: int = 300
    run_max_sonnet: int = 40
    judge_timeout_s: float = 120.0
    claude_bin: str = "claude"
    # Not derived from data_dir: the run caps count every row ever, so moving data_dir must not reset them.
    # AUTOCLIPPER_LEDGER exists for tests (Guard refuses the default path under pytest).
    ledger_path: Path = PROJECT_ROOT / "data" / "judge-ledger.sqlite"
    needs_jay_path: Path = PROJECT_ROOT / ".autopilot" / "NEEDS-JAY.md"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_vision: bool = False  # True: qwen3.8-27b on frames (slow, spills VRAM); False: qwen3:8b on text
    ollama_vision_model: str = "qwen3.8-27b"
    ollama_text_model: str = "qwen3:8b"
    discord_test_webhook_url: str = ""
    # Code-enforced Discord caps: clips counted persistently (all processes), texts per run.
    post_max_clips: int = 5
    post_max_texts: int = 20

    @classmethod
    def from_env(cls, dotenv: bool = True) -> Settings:
        if dotenv:
            load_dotenv()
        e = os.environ.get
        d = cls()
        return cls(
            host=e("AUTOCLIPPER_HOST", d.host),
            port=int(e("AUTOCLIPPER_PORT", d.port)),
            replay_dir=Path(e("AUTOCLIPPER_REPLAY_DIR", str(d.replay_dir))),
            output_dir=Path(e("AUTOCLIPPER_OUTPUT_DIR", str(d.output_dir))),
            data_dir=Path(e("AUTOCLIPPER_DATA_DIR", str(d.data_dir))),
            judge=e("AUTOCLIPPER_JUDGE", d.judge),
            judge_model=e("AUTOCLIPPER_JUDGE_MODEL", d.judge_model),
            polish_model=e("AUTOCLIPPER_POLISH_MODEL", d.polish_model),
            judge_max_calls_per_hour=int(
                e("AUTOCLIPPER_JUDGE_MAX_CALLS_PER_HOUR", d.judge_max_calls_per_hour)
            ),
            polish_max_calls_per_day=int(
                e("AUTOCLIPPER_POLISH_MAX_CALLS_PER_DAY", d.polish_max_calls_per_day)
            ),
            judge_max_5h=float(e("AUTOCLIPPER_JUDGE_MAX_5H", d.judge_max_5h)),
            judge_max_7d=float(e("AUTOCLIPPER_JUDGE_MAX_7D", d.judge_max_7d)),
            run_max_haiku=int(e("AUTOCLIPPER_RUN_MAX_HAIKU", d.run_max_haiku)),
            run_max_sonnet=int(e("AUTOCLIPPER_RUN_MAX_SONNET", d.run_max_sonnet)),
            judge_timeout_s=float(e("AUTOCLIPPER_JUDGE_TIMEOUT", d.judge_timeout_s)),
            claude_bin=e("AUTOCLIPPER_CLAUDE_BIN", d.claude_bin),
            ledger_path=Path(e("AUTOCLIPPER_LEDGER", str(d.ledger_path))),
            needs_jay_path=Path(e("AUTOCLIPPER_NEEDS_JAY", str(d.needs_jay_path))),
            ollama_url=e("AUTOCLIPPER_OLLAMA_URL", d.ollama_url),
            ollama_vision=e("AUTOCLIPPER_OLLAMA_VISION", "1" if d.ollama_vision else "0").lower()
            in ("1", "true", "yes", "on"),
            ollama_vision_model=e("AUTOCLIPPER_OLLAMA_VISION_MODEL", d.ollama_vision_model),
            ollama_text_model=e("AUTOCLIPPER_OLLAMA_TEXT_MODEL", d.ollama_text_model),
            discord_test_webhook_url=e("DISCORD_TEST_WEBHOOK_URL", d.discord_test_webhook_url),
            post_max_clips=int(e("AUTOCLIPPER_POST_MAX_CLIPS", d.post_max_clips)),
            post_max_texts=int(e("AUTOCLIPPER_POST_MAX_TEXTS", d.post_max_texts)),
        )


def bind_socket(host: str, port: int) -> socket.socket:
    """Bind a listening socket, failing loudly if the port is taken (no fallback to another port)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        raise SystemExit(
            f"autoclipper: {host}:{port} is taken ({exc}); check `ports` for the conflict"
        ) from exc
    sock.listen()
    return sock
