"""Local fallback judge on Windows ollama. Never pulls models: a missing model is reported as unavailable."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..config import Settings

TEXT_TIMEOUT_S = 180.0
VISION_TIMEOUT_S = 600.0


@dataclass
class OllamaResult:
    ok: bool
    model: str
    data: dict | None = None
    error: str = ""
    unavailable: bool = False
    duration_s: float = 0.0


def _get_json(url: str, payload: dict | None, timeout: float) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _has_model(names: list[str], model: str) -> bool:
    want = model if ":" in model else f"{model}:latest"
    return any(n == model or n == want for n in names)


def _parse(content: str) -> dict:
    content = content.strip()
    if "</think>" in content:
        content = content.split("</think>", 1)[1].strip()
    if content.startswith("```"):
        content = content.strip("`").removeprefix("json").strip()
    d = json.loads(content)
    if not isinstance(d, dict):
        raise ValueError("answer is not a JSON object")
    return d


def ollama_judge(
    settings: Settings, system: str, text: str, images_b64: list[str], schema: dict
) -> OllamaResult:
    """One structured-output chat call. Vision model on frames if `ollama_vision`, else text model."""
    vision = settings.ollama_vision and bool(images_b64)
    model = settings.ollama_vision_model if vision else settings.ollama_text_model
    base = settings.ollama_url.rstrip("/")
    t0 = time.monotonic()

    def done(**kw: object) -> OllamaResult:
        return OllamaResult(model=model, duration_s=round(time.monotonic() - t0, 3), **kw)  # type: ignore[arg-type]

    try:
        names = [m.get("name", "") for m in _get_json(f"{base}/api/tags", None, 5).get("models", [])]
    except (OSError, urllib.error.URLError, ValueError) as e:
        return done(ok=False, unavailable=True, error=f"ollama unreachable at {base}: {e}")
    if not _has_model(names, model):
        return done(
            ok=False, unavailable=True, error=f"ollama model {model} is not installed (not pulling it)"
        )

    user: dict = {"role": "user", "content": text}
    if vision:
        user["images"] = images_b64
    else:
        user["content"] = "(No frames: judge from the events and transcript only.)\n" + text
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, user],
        "format": schema,
        "stream": False,
        "think": False,
        "options": {"temperature": 0},
    }
    try:
        r = _get_json(f"{base}/api/chat", payload, VISION_TIMEOUT_S if vision else TEXT_TIMEOUT_S)
        return done(ok=True, data=_parse(r["message"]["content"]))
    except (OSError, urllib.error.URLError, KeyError, TypeError, ValueError) as e:
        return done(ok=False, error=f"ollama {model} failed: {type(e).__name__}: {e}"[:300])
