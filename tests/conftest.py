from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Directory with the generated media fixtures (made once per session, skipped if present)."""
    if shutil.which("ffmpeg") is None or shutil.which("espeak-ng") is None:
        pytest.skip("ffmpeg and espeak-ng are needed to generate media fixtures")
    spec = importlib.util.spec_from_file_location("make_fixtures", FIXTURES / "make_fixtures.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = FIXTURES / "generated"
    mod.generate(out)
    return out


@pytest.fixture(scope="session")
def eft_logs_dir() -> Path:
    """Redacted EFT log excerpts (log_<date>_<ver>/<stamp> application_000.log)."""
    return FIXTURES / "eft"
