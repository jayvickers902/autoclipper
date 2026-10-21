"""AudioSet tags per second with PANNs CNN14 (ONNX, onnxruntime on the CPU).

`PannsTagger()` is the `tagger(samples, sr, role) -> list[dict]` hook of `analyze`: one dict per whole
second, `{tag: score}` with tag in GROUPS (gunshot, explosion, laughter, shout, speech, music) and score the
max AudioSet probability over that group's classes, from the 2 s window centred on that second. Scores under
MIN_SCORE are left out, silent seconds get `{}`.

The model is the PANNs Cnn14 (16 kHz variant, log-mel front end inside the graph) exported to ONNX and
published at huggingface.co/pranjal-pravesh/PANNs_CNN14_ONNX (weights from the PANNs authors' AudioSet
checkpoint). It is 328 MB, downloaded once to AUTOCLIPPER_CACHE (default ~/.cache/autoclipper/panns) and
checked against the sha256 pinned below. No torch, no CUDA: about 2.8 s per 86 s track on the CPU.
"""

from __future__ import annotations

import csv
import hashlib
import os
import threading
from pathlib import Path

import numpy as np

MODEL_NAME = "panns-cnn14-16k"
_HF = "https://huggingface.co/pranjal-pravesh/PANNs_CNN14_ONNX/resolve/main"
# file name -> (url, sha256)
FILES: dict[str, tuple[str, str]] = {
    "Cnn14_16k.onnx": (
        f"{_HF}/Cnn14_16k.onnx",
        "ad56b79861aaecc29a27e3e47923954de81418a181135f4d94de8d1a15e0fa9c",
    ),
    "Cnn14_16k.onnx.data": (  # external weights of the .onnx graph; must sit next to it
        f"{_HF}/Cnn14_16k.onnx.data",
        "d95f724232bb60b78c8e9ef3e031a599e00be33e8347fbbd6472f6e1348e6a8b",
    ),
    "class_labels_indices.csv": (  # AudioSet ontology labels (527 classes, row order = model output order)
        "http://storage.googleapis.com/us_audioset/youtube_corpus/v1/csv/class_labels_indices.csv",
        "cdd1049833c4b86127c2773ac0d14a2754b6a6d0d1798002ed5c66e699708429",
    ),
}

SR = 16_000
WINDOW_S = 2.0  # context per second: [t - 0.5, t + 1.5)
BATCH = 16
MIN_SCORE = 0.02
SILENT_DB = -75.0  # seconds quieter than this (and with a quiet window) are not run through the model

# tag -> AudioSet display names whose probabilities are max-ed together
GROUPS: dict[str, tuple[str, ...]] = {
    "gunshot": ("Gunshot, gunfire", "Machine gun", "Fusillade", "Cap gun"),
    "explosion": ("Explosion", "Boom", "Artillery fire", "Burst, pop"),
    "laughter": ("Laughter", "Giggle", "Belly laugh", "Chuckle, chortle", "Baby laughter", "Snicker"),
    "shout": ("Shout", "Yell", "Bellow", "Battle cry", "Screaming", "Children shouting", "Whoop"),
    "speech": ("Speech",),
    "music": ("Music",),
}


def cache_dir() -> Path:
    return Path(os.environ.get("AUTOCLIPPER_CACHE", "~/.cache/autoclipper")).expanduser() / "panns"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def ensure_files(directory: Path | None = None) -> Path:
    """Download (once) and verify the model files; returns the directory. Raises on a sha256 mismatch."""
    import httpx  # already a dependency; only needed on the first run

    d = directory or cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name, (url, sha) in FILES.items():
        dest = d / name
        if dest.is_file() and _sha256(dest) == sha:
            continue
        tmp = dest.with_suffix(dest.suffix + ".part")
        with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        if (got := _sha256(tmp)) != sha:
            tmp.unlink()
            raise RuntimeError(f"{name}: sha256 {got} does not match the pinned {sha}")
        tmp.replace(dest)
    return d


def window_batches(samples: np.ndarray, n_seconds: int, batch: int = BATCH):
    """Yield (first_second, array [b, WINDOW_S * SR]) with the window around each second, zero padded."""
    half = int(0.5 * SR)
    length = int(WINDOW_S * SR)
    padded = np.concatenate([np.zeros(half, np.float32), samples.astype(np.float32, copy=False),
                             np.zeros(length + SR, np.float32)])  # fmt: skip
    for s0 in range(0, n_seconds, batch):
        secs = range(s0, min(n_seconds, s0 + batch))
        yield s0, np.stack([padded[s * SR : s * SR + length] for s in secs])


class PannsTagger:
    """Callable `tagger(samples, sr, role)`; the ONNX session is created on first use and shared."""

    name = MODEL_NAME

    def __init__(self, directory: Path | None = None, threads: int | None = None) -> None:
        self._dir = directory
        self._threads = threads or int(os.environ.get("AUTOCLIPPER_TAGGER_THREADS", "0")) or min(
            os.cpu_count() or 4, 8
        )
        self._lock = threading.Lock()
        self._session = None
        self._index: dict[str, list[int]] = {}

    def _load(self):
        with self._lock:
            if self._session is None:
                import onnxruntime as ort

                d = ensure_files(self._dir)
                with (d / "class_labels_indices.csv").open(newline="", encoding="utf-8") as f:
                    names = [row["display_name"] for row in csv.DictReader(f)]
                self._index = {
                    tag: sorted({names.index(n) for n in labels if n in names})
                    for tag, labels in GROUPS.items()
                }
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = self._threads
                opts.log_severity_level = 3  # the graph declares batch 1 and warns on every larger batch
                self._session = ort.InferenceSession(
                    str(d / "Cnn14_16k.onnx"), opts, providers=["CPUExecutionProvider"]
                )
        return self._session

    def __call__(self, samples: np.ndarray, sr: int, role: str = "") -> list[dict]:
        if sr != SR:
            raise ValueError(f"PannsTagger needs {SR} Hz audio, got {sr}")
        n_seconds = max(1, int(np.ceil(samples.size / SR)))
        out: list[dict] = [{} for _ in range(n_seconds)]
        session = self._load()
        # skip digital silence (a muted track): the model would only report its noise floor
        db = 20 * np.log10(np.maximum(_second_peaks(samples, n_seconds), 1e-9))
        for s0, batch in window_batches(samples, n_seconds):
            live = [i for i in range(len(batch)) if db[s0 + i] > SILENT_DB]
            if not live:
                continue
            (scores, _emb) = session.run(None, {"input_audio": batch[live]})
            for row, i in zip(scores, live, strict=True):
                tags = {t: round(float(row[idx].max()), 3) for t, idx in self._index.items()}
                out[s0 + i] = {t: v for t, v in tags.items() if v >= MIN_SCORE}
        return out


def _second_peaks(samples: np.ndarray, n_seconds: int) -> np.ndarray:
    pad = np.zeros(n_seconds * SR, np.float32)
    pad[: samples.size] = np.abs(samples[: n_seconds * SR])
    return pad.reshape(n_seconds, SR).max(axis=1)
