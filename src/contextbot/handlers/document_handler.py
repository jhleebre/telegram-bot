"""Document handler — Phase 2 stub.

Phase 2 will:
- pdf/pptx/docx/xlsx/… → convert to Markdown in the inbox, move the original to ~/Downloads/
- .md → save the file as-is directly to the inbox

See docs/PHASE2.md.
"""

from __future__ import annotations

from ..config import Settings
from .base import HandlerResult, IncomingMessage, MessageKind


async def handle_document(message: IncomingMessage, settings: Settings) -> HandlerResult:
    if message.kind == MessageKind.MARKDOWN:
        return HandlerResult(
            reply="🚧 마크다운 파일 저장 기능은 Phase 2에서 지원 예정입니다."
        )
    return HandlerResult(
        reply="🚧 문서(pdf/pptx/docx 등) → 마크다운 변환은 Phase 2에서 지원 예정입니다."
    )
