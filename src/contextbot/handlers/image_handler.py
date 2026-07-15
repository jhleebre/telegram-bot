"""Image handler — Phase 2 stub.

Phase 2 will describe the image (VLM) with OCR and produce a Markdown note embedding the
original image (see docs/PHASE2.md).
"""

from __future__ import annotations

from ..config import Settings
from .base import HandlerResult, IncomingMessage


async def handle_image(message: IncomingMessage, settings: Settings) -> HandlerResult:
    return HandlerResult(
        reply="🚧 이미지 → 설명/OCR 노트 기능은 Phase 2에서 지원 예정입니다."
    )
