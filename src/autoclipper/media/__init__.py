"""Pure ffmpeg/ffprobe media primitives (subprocess only; originals are never written to)."""

from ._run import MediaError
from .audio import audio_tracks, extract_audio, extract_audio_window
from .cut import CutResult, cut_copy
from .detect import Interval, detect_black, detect_frozen, is_dead_capture, is_dead_sampled
from .encode import EncodeResult, nvenc_works, share_encode
from .preview import preview_webp, thumbnail
from .probe import AudioStream, MediaInfo, keyframe_times, probe

__all__ = [
    "AudioStream", "CutResult", "EncodeResult", "Interval", "MediaError", "MediaInfo",
    "audio_tracks", "cut_copy", "detect_black", "detect_frozen", "extract_audio", "extract_audio_window",
    "is_dead_capture", "is_dead_sampled", "keyframe_times", "nvenc_works", "preview_webp", "probe",
    "share_encode", "thumbnail",
]  # fmt: skip
