"""Audio track extraction to analysis-ready WAV."""

from __future__ import annotations

from pathlib import Path

from ._run import MediaError, ffmpeg, prepare_output
from .probe import AudioStream, probe


def audio_tracks(path: Path | str) -> list[AudioStream]:
    return probe(path).audio


def extract_audio(path: Path | str, stream_index: int | None, out_wav: Path | str) -> Path:
    """Write 16 kHz mono s16 WAV. `stream_index` is the absolute stream index (AudioStream.index);
    None picks the first audio track (the mix)."""
    out = prepare_output(path, out_wav)
    tracks = audio_tracks(path)
    if not tracks:
        raise MediaError(f"no audio streams in {path}")
    if stream_index is None:
        stream_index = tracks[0].index
    elif stream_index not in {t.index for t in tracks}:
        raise MediaError(f"stream {stream_index} is not an audio stream of {path}")
    ffmpeg("-i", str(path), "-map", f"0:{stream_index}", "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", "-f", "wav", str(out))  # fmt: skip
    return out


def extract_audio_window(path: Path | str, start: float, length: float, out: Path | str) -> Path:
    """Every audio stream of [start, start + length) as 16 kHz mono PCM in one Matroska file (stream order
    kept), so a chunk of a long recording can be analysed like a short replay. Accurate input seek."""
    out_p = prepare_output(path, out)
    if not audio_tracks(path):
        raise MediaError(f"no audio streams in {path}")
    ffmpeg("-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(path), "-map", "0:a", "-vn",
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "matroska", str(out_p))  # fmt: skip
    return out_p
