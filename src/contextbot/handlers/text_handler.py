"""Phase 1 text handler: a text memo becomes a Markdown note in the inbox."""

from __future__ import annotations

from ..config import Settings
from ..notes.markdown_writer import write_note
from .base import HandlerResult, IncomingMessage

_TITLE_MAX_LEN = 80


def _derive_title(text: str) -> str:
    """Use the first non-empty line, trimmed, as the note title."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            if len(stripped) > _TITLE_MAX_LEN:
                return stripped[:_TITLE_MAX_LEN].rstrip() + "…"
            return stripped
    return "Untitled note"


async def handle_text(message: IncomingMessage, settings: Settings) -> HandlerResult:
    """Write the message text to a Markdown note and report the saved filename."""
    text = message.text.strip()
    if not text:
        return HandlerResult(reply="빈 메시지는 저장하지 않았습니다.")

    title = _derive_title(text)
    path = write_note(
        inbox_dir=settings.inbox_dir,
        body=text,
        title=title,
        when=message.date,
        source="telegram",
        note_type="note",
        tags=[],
        extra={"telegram_message_id": message.message_id},
        slug_source=title,
    )
    return HandlerResult(reply=f"📝 저장됨: {path.name}", saved_path=path)
