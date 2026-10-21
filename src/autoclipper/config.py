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
    ollama_url: str = "http://127.0.0.1:11434"
    discord_test_webhook_url: str = ""

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
            ollama_url=e("AUTOCLIPPER_OLLAMA_URL", d.ollama_url),
            discord_test_webhook_url=e("DISCORD_TEST_WEBHOOK_URL", d.discord_test_webhook_url),
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
