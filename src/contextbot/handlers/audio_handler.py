"""Audio handler — Phase 2 stub.

Phase 2 will download the audio, run local Whisper transcription, apply glossary corrections,
and generate a meeting note via a human-in-the-loop review (see docs/PHASE2.md).
"""

from __future__ import annotations

from ..config import Settings
from .base import HandlerResult, IncomingMessage


async def handle_audio(message: IncomingMessage, settings: Settings) -> HandlerResult:
    return HandlerResult(
        reply="🚧 오디오 → 회의록 기능은 Phase 2에서 지원 예정입니다."
    )
