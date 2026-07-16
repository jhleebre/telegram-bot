"""Local speech-to-text (`mlx-whisper`, Apple Silicon).

Ported from `~/Projects/meeting-transcriber/`, deliberately **not imported from it**: that project
is the source to read, not a runtime dependency (docs/PHASE2.md, increment 5). The bot stands alone.
"""

from .whisper import (
    DEFAULT_LANGUAGE,
    DEFAULT_MODEL,
    Transcript,
    TranscriptionError,
    format_timestamp,
    model_is_cached,
    resolve_ffmpeg,
    transcribe,
)

__all__ = [
    "DEFAULT_LANGUAGE",
    "DEFAULT_MODEL",
    "Transcript",
    "TranscriptionError",
    "format_timestamp",
    "model_is_cached",
    "resolve_ffmpeg",
    "transcribe",
]
