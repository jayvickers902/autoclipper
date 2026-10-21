"""Per-second signal timeline for one replay (audio only; AudioSet tags and GPU whisper come later).

`analyze(path, out_json=None)` returns (and optionally writes) this JSON-able dict, `version` 1:

    {
      "version": 1,
      "source": "/path/to/replay.mp4",
      "duration": 59.2,                       # seconds, from ffprobe
      "dead": false,                          # true: black/frozen capture, no audio work was done
      "dead_reason": "",                      # is_dead_capture's reason when dead
      "tracks": [{"order": 0, "index": 1, "role": "mix"}, ...],   # roles: mix/game/discord/mic or track_N
      "sources": {"transients": "game", "speech": ["discord", "mic"]},   # roles actually used
      "whisper_model": "large-v3-turbo",      # "" when speech was not run
      "seconds": {                            # role -> one entry per whole second 0..ceil(duration)-1
        "game": [{"t": 0, "rms_db": -41.2, "peak_db": -12.0, "onsets": 0, "speech": false, "tags": {}}, ...]
      },
      "events": [                             # sorted by t
        {"t": 6.0, "kind": "transient", "strength": 0.86, "role": "game"},
        {"t": 3.0, "kind": "keyword", "phrase": "clip that", "text": "Clip that.", "end": 3.6, "role": "mic"}
      ],
      "transcript": [{"role": "mic", "start": 2.9, "end": 3.6, "text": "Clip that.",
                      "words": [{"start": 2.9, "end": 3.2, "word": "Clip", "p": 0.9}, ...]}]
    }

dB values are dBFS (-90 = silence). `onsets` counts transients (10 ms resolution, 40 ms refractory) inside
that second on that track; only the transient-source role also emits "transient" events, so voice tracks'
onset counts include plosives and are not events. `speech` is true when a transcribed word overlaps the
second (always false for tracks that are not transcribed). `tags` is the slot for AudioSet labels
(`{label: score}`), filled by an optional `tagger(samples, sr, role) -> list[dict]` (one dict per second).
"""

from __future__ import annotations

import json
import math
import tempfile
from collections.abc import Callable
from pathlib import Path

import numpy as np

from ..media import audio_tracks, extract_audio, is_dead_capture, probe
from . import keywords, loudness, speech

VERSION = 1
FOUR_TRACK_ROLES = ("mix", "game", "discord", "mic")  # OBS layout: tracks 1-4

Tagger = Callable[[np.ndarray, int, str], list[dict]]


def assign_roles(n_tracks: int) -> list[str]:
    if n_tracks == 4:
        return list(FOUR_TRACK_ROLES)
    return ["mix" if i == 0 else f"track_{i + 1}" for i in range(n_tracks)]


def _speech_seconds(segments: list[dict], n_seconds: int) -> list[bool]:
    flags = [False] * n_seconds
    for seg in segments:
        for w in seg["words"]:
            for s in range(int(w["start"]), min(int(w["end"]) + 1, n_seconds)):
                flags[s] = True
    return flags


def analyze(
    path: Path | str,
    out_json: Path | str | None = None,
    *,
    whisper: bool = True,
    whisper_model: str | None = None,
    bias_keywords: bool = False,
    check_dead: bool = True,
    tagger: Tagger | None = None,
) -> dict:
    """Build the signal timeline for `path` (see the module docstring); write it to `out_json` if given.

    `bias_keywords` feeds the KEYWORDS list to whisper as its initial prompt (better recall from small
    models, some risk of echoing the prompt on noise); off by default.
    """
    src = Path(path)
    info = probe(src)
    streams = audio_tracks(src)
    roles = assign_roles(len(streams))
    result: dict = {
        "version": VERSION,
        "source": str(src),
        "duration": round(info.duration, 3),
        "dead": False,
        "dead_reason": "",
        "tracks": [
            {"order": s.order, "index": s.index, "role": r} for s, r in zip(streams, roles, strict=True)
        ],
        "sources": {"transients": None, "speech": []},
        "whisper_model": "",
        "seconds": {},
        "events": [],
        "transcript": [],
    }
    dead, reason = is_dead_capture(src) if check_dead else (False, "")
    if dead:
        result.update(dead=True, dead_reason=reason)
    elif streams:
        _fill(result, src, streams, roles, whisper, whisper_model, bias_keywords, tagger)
    if out_json is not None:
        out = Path(out_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return result


def _fill(result, src, streams, roles, whisper, whisper_model, bias_keywords, tagger) -> None:
    transient_role = "game" if "game" in roles else "mix"
    speech_roles = [r for r in ("discord", "mic") if r in roles] or ["mix"]
    result["sources"] = {"transients": transient_role, "speech": speech_roles if whisper else []}
    audio: dict[str, np.ndarray] = {}
    sr = 16_000
    with tempfile.TemporaryDirectory(prefix="autoclipper-signals-") as tmp:
        for stream, role in zip(streams, roles, strict=True):
            wav = extract_audio(src, stream.index, Path(tmp) / f"{role}.wav")
            audio[role], sr = loudness.read_wav(wav)
    n_seconds = max(1, math.ceil(max(result["duration"], max(a.size for a in audio.values()) / sr) - 0.05))
    events: list[dict] = []
    for role, samples in audio.items():
        rms, peak = loudness.per_second_levels(samples, sr, n_seconds)
        transients = loudness.detect_transients(samples, sr)
        onsets = loudness.onset_counts(transients, n_seconds)
        tags = tagger(samples, sr, role) if tagger else []
        result["seconds"][role] = [
            {
                "t": s,
                "rms_db": round(float(rms[s]), 1),
                "peak_db": round(float(peak[s]), 1),
                "onsets": onsets[s],
                "speech": False,
                "tags": tags[s] if s < len(tags) else {},
            }
            for s in range(n_seconds)
        ]
        if role == transient_role:
            events += [{"t": t, "kind": "transient", "strength": st, "role": role} for t, st in transients]
    if whisper:
        prompt = ", ".join(keywords.KEYWORDS) if bias_keywords else None
        result["whisper_model"] = whisper_model or speech.model_name()
        for role in speech_roles:
            segments = speech.transcribe(audio[role], role, whisper_model, prompt)
            result["transcript"] += segments
            for s, flag in enumerate(_speech_seconds(segments, n_seconds)):
                result["seconds"][role][s]["speech"] = flag
            words = [w for seg in segments for w in seg["words"]]
            events += [{"kind": "keyword", "role": role, **k} for k in keywords.find_keywords(words)]
    result["events"] = sorted(events, key=lambda e: e["t"])
