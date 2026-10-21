"""Speech-to-text with faster-whisper (CPU, int8). The model is loaded lazily, once per process."""

from __future__ import annotations

import os
import threading

import numpy as np

DEFAULT_MODEL = "large-v3-turbo"
_lock = threading.Lock()
_models: dict[str, object] = {}


def model_name() -> str:
    return os.environ.get("AUTOCLIPPER_WHISPER_MODEL", DEFAULT_MODEL)


def get_model(name: str | None = None):
    """The cached WhisperModel for `name` (default AUTOCLIPPER_WHISPER_MODEL). Downloads to the HF cache."""
    name = name or model_name()
    with _lock:
        if name not in _models:
            from faster_whisper import WhisperModel  # heavy import: only when speech is wanted

            threads = int(os.environ.get("AUTOCLIPPER_WHISPER_THREADS", "0")) or min(os.cpu_count() or 4, 12)
            _models[name] = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads)
        return _models[name]


def transcribe(
    samples: np.ndarray, role: str, model: str | None = None, prompt: str | None = None
) -> list[dict]:
    """16 kHz mono float32 -> segments [{"role","start","end","text","words":[{"start","end","word","p"}]}].

    VAD-filtered with word timestamps. Language defaults to English (AUTOCLIPPER_WHISPER_LANG, "" = detect).
    `prompt` is whisper's initial_prompt: listing the keyword phrases helps small models spell them.
    """
    lang = os.environ.get("AUTOCLIPPER_WHISPER_LANG", "en") or None
    segments, _info = get_model(model).transcribe(
        samples.astype(np.float32, copy=False),
        language=lang,
        vad_filter=True,
        word_timestamps=True,
        condition_on_previous_text=False,
        initial_prompt=prompt,
    )
    out = []
    for seg in segments:
        words = [
            {"start": float(round(w.start, 2)), "end": float(round(w.end, 2)), "word": w.word.strip(),
             "p": float(round(w.probability, 2))}
            for w in (seg.words or [])
        ]
        out.append(
            {"role": role, "start": float(round(seg.start, 2)), "end": float(round(seg.end, 2)),
             "text": seg.text.strip(), "words": words}
        )
    return out
