import socket

import pytest

from autoclipper.cli import main
from autoclipper.config import DEFAULT_PORT, Settings, bind_socket, load_dotenv


def test_defaults_use_registered_port(monkeypatch):
    monkeypatch.delenv("AUTOCLIPPER_PORT", raising=False)
    s = Settings.from_env(dotenv=False)
    assert s.port == DEFAULT_PORT == 4180
    assert s.host == "127.0.0.1"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("AUTOCLIPPER_JUDGE_MAX_5H", "0.5")
    assert Settings.from_env(dotenv=False).judge_max_5h == 0.5


def test_dotenv_does_not_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# c\nAUTOCLIPPER_X_TEST=from_file\nAUTOCLIPPER_Y_TEST='quoted'\n")
    monkeypatch.setenv("AUTOCLIPPER_X_TEST", "from_env")
    monkeypatch.delenv("AUTOCLIPPER_Y_TEST", raising=False)
    load_dotenv(env)
    import os

    assert os.environ["AUTOCLIPPER_X_TEST"] == "from_env"
    assert os.environ["AUTOCLIPPER_Y_TEST"] == "quoted"
    monkeypatch.delenv("AUTOCLIPPER_Y_TEST")


def test_bind_fails_when_taken():
    first = bind_socket("127.0.0.1", 0)
    port = first.getsockname()[1]
    try:
        with pytest.raises(SystemExit):
            bind_socket("127.0.0.1", port)
    finally:
        first.close()
    assert isinstance(first, socket.socket)


def test_cli_help():
    assert main([]) == 0
