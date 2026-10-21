"""Speech-to-text with faster-whisper. Models are loaded lazily, once per process and device.

Device: CUDA (float16) when ctranslate2 sees a GPU, the cuBLAS/cuDNN libraries load, and `GpuGate` says no
game is running; CPU int8 otherwise. AUTOCLIPPER_WHISPER_DEVICE=cpu forces the CPU. A CUDA failure at load or
run time falls back to the CPU for the rest of the process. In WSL the driver lib lives in /usr/lib/wsl/lib
(already on the loader path); cuBLAS 12 and cuDNN 9 come from the optional `gpu` extra
(`uv sync --extra gpu`: nvidia-cublas-cu12 + nvidia-cudnn-cu12, ~1.3 GB) and are preloaded by
`_load_cuda_libs`.
"""

from __future__ import annotations

import ctypes
import gc
import importlib.util
import os
import threading
from pathlib import Path

import numpy as np

from ..gpu_gate import GpuGate

DEFAULT_MODEL = "large-v3-turbo"
_lock = threading.Lock()
_models: dict[tuple[str, str], object] = {}
_gate: GpuGate | None = None
_cuda_ok: bool | None = None  # None = not probed yet
last_device = ""  # device of the most recent transcribe() call


def model_name() -> str:
    return os.environ.get("AUTOCLIPPER_WHISPER_MODEL", DEFAULT_MODEL)


def _load_cuda_libs() -> bool:
    """Preload pip-installed cuBLAS/cuDNN (nvidia-*-cu12 wheels) so ctranslate2 finds them. True if usable."""
    for pkg, names in (
        ("nvidia.cublas", ("libcublasLt.so.12", "libcublas.so.12")),
        ("nvidia.cudnn", ("libcudnn.so.9",)),
    ):
        try:
            spec = importlib.util.find_spec(pkg)
        except (ImportError, ValueError):
            spec = None
        if spec is None or not spec.submodule_search_locations:
            return False
        lib = Path(next(iter(spec.submodule_search_locations))) / "lib"
        try:
            for n in names:
                ctypes.CDLL(str(lib / n), mode=ctypes.RTLD_GLOBAL)
        except OSError:
            return False
    return True


def cuda_available() -> bool:
    """ctranslate2 sees a GPU and the CUDA libraries load (probed once per process)."""
    global _cuda_ok
    if _cuda_ok is None:
        try:
            import ctranslate2

            _cuda_ok = ctranslate2.get_cuda_device_count() > 0 and _load_cuda_libs()
        except Exception:  # noqa: BLE001 - no GPU, no driver, broken install: all mean "use the CPU"
            _cuda_ok = False
    return _cuda_ok


def choose_device() -> str:
    """"cuda" when the GPU is usable and no game runs, else "cpu" (and a resident CUDA model is dropped)."""
    global _gate
    if os.environ.get("AUTOCLIPPER_WHISPER_DEVICE", "auto").lower() == "cpu" or not cuda_available():
        return "cpu"
    _gate = _gate or GpuGate()
    if _gate.games_running():
        with _lock:
            for key in [k for k in _models if k[1] == "cuda"]:
                del _models[key]  # give the VRAM back to the game
            gc.collect()
        return "cpu"
    return "cuda"


def get_model(name: str | None = None, device: str = "cpu"):
    """The cached WhisperModel for `name` (default AUTOCLIPPER_WHISPER_MODEL) on `device` ("cpu"/"cuda")."""
    name = name or model_name()
    with _lock:
        if (name, device) not in _models:
            from faster_whisper import WhisperModel  # heavy import: only when speech is wanted

            if device == "cuda":
                _models[name, device] = WhisperModel(name, device="cuda", compute_type="float16")
            else:
                threads = int(os.environ.get("AUTOCLIPPER_WHISPER_THREADS", "0")) or min(
                    os.cpu_count() or 4, 12
                )
                _models[name, device] = WhisperModel(
                    name, device="cpu", compute_type="int8", cpu_threads=threads
                )
        return _models[name, device]


def transcribe(
    samples: np.ndarray, role: str, model: str | None = None, prompt: str | None = None
) -> list[dict]:
    """16 kHz mono float32 -> segments [{"role","start","end","text","words":[{"start","end","word","p"}]}].

    VAD-filtered with word timestamps. Language defaults to English (AUTOCLIPPER_WHISPER_LANG, "" = detect).
    `prompt` is whisper's initial_prompt: listing the keyword phrases helps small models spell them.
    """
    global _cuda_ok, last_device
    lang = os.environ.get("AUTOCLIPPER_WHISPER_LANG", "en") or None
    audio = samples.astype(np.float32, copy=False)
    device = choose_device()
    while True:
        try:
            segments, _info = get_model(model, device).transcribe(
                audio,
                language=lang,
                vad_filter=True,
                word_timestamps=True,
                condition_on_previous_text=False,
                initial_prompt=prompt,
            )
            segments = list(segments)  # decoding is lazy: CUDA errors surface here
            break
        except RuntimeError:
            if device == "cpu":
                raise
            _cuda_ok, device = False, "cpu"  # cuBLAS/cuDNN missing or out of VRAM: CPU for this process
    last_device = device
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
