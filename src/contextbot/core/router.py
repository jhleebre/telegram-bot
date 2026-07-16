"""Message classification and dispatch.

The routing table (message kind → handler) is defined here so Phase 2 only needs to swap the stub
handlers for real ones. Classification is pure and unit-testable; ``build_incoming_message`` adapts
a Telethon ``Message`` via duck typing so tests can use simple fakes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from ..config import Settings
from ..handlers.audio_handler import handle_audio
from ..handlers.base import HandlerResult, IncomingMessage, MessageKind
from ..handlers.conversation import handle_review_request, is_review_request
from ..handlers.document_handler import handle_document
from ..handlers.image_handler import handle_image
from ..handlers.text_handler import handle_text

logger = logging.getLogger("contextbot.router")

HandlerFn = Callable[[IncomingMessage, Settings], Awaitable[HandlerResult]]

# Message kind → handler. Phase 2 replaces the stubbed handlers.
ROUTING_TABLE: dict[MessageKind, HandlerFn] = {
    MessageKind.TEXT: handle_text,
    MessageKind.AUDIO: handle_audio,
    MessageKind.IMAGE: handle_image,
    MessageKind.DOCUMENT: handle_document,
    MessageKind.MARKDOWN: handle_document,
}

_AUDIO_EXTS = {".m4a", ".wav", ".mp3", ".ogg", ".oga", ".flac", ".aac"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif"}
_MARKDOWN_EXTS = {".md", ".markdown"}
_DOCUMENT_EXTS = {".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xls", ".txt", ".csv"}


def _ext(file_name: Optional[str]) -> str:
    if not file_name or "." not in file_name:
        return ""
    return "." + file_name.rsplit(".", 1)[1].lower()


def classify_document(file_name: Optional[str], mime_type: Optional[str]) -> MessageKind:
    """Classify a file attachment by extension first, then MIME type."""
    ext = _ext(file_name)
    if ext in _MARKDOWN_EXTS:
        return MessageKind.MARKDOWN
    if ext in _AUDIO_EXTS:
        return MessageKind.AUDIO
    if ext in _IMAGE_EXTS:
        return MessageKind.IMAGE
    if ext in _DOCUMENT_EXTS:
        return MessageKind.DOCUMENT

    mime = (mime_type or "").lower()
    if mime.startswith("audio/"):
        return MessageKind.AUDIO
    if mime.startswith("image/"):
        return MessageKind.IMAGE
    if mime in {"text/markdown", "text/x-markdown"}:
        return MessageKind.MARKDOWN
    if mime:
        return MessageKind.DOCUMENT
    return MessageKind.UNKNOWN


def build_incoming_message(message) -> Optional[IncomingMessage]:
    """Adapt a Telethon ``Message`` into an :class:`IncomingMessage`, or None if empty.

    Uses duck typing (Telethon's convenience properties: ``voice``/``audio``/``photo``/
    ``document``/``file``) so tests can pass lightweight fakes.
    """
    if message is None:
        return None

    message_id = getattr(message, "id", 0)
    date = getattr(message, "date", None) or datetime.now(timezone.utc)
    text = getattr(message, "raw_text", None) or getattr(message, "message", None) or ""
    sender_id = getattr(message, "sender_id", None)
    chat_id = getattr(message, "chat_id", None) or 0

    file = getattr(message, "file", None)
    file_name = getattr(file, "name", None) if file is not None else None
    mime_type = getattr(file, "mime_type", None) if file is not None else None

    if getattr(message, "voice", None) is not None:
        kind = MessageKind.AUDIO
    elif getattr(message, "audio", None) is not None:
        kind = MessageKind.AUDIO
    elif getattr(message, "photo", None) is not None:
        kind = MessageKind.IMAGE
    elif getattr(message, "document", None) is not None:
        kind = classify_document(file_name, mime_type)
    elif text:
        kind = MessageKind.TEXT
    else:
        kind = MessageKind.UNKNOWN

    return IncomingMessage(
        user_id=sender_id,
        chat_id=chat_id,
        message_id=message_id,
        date=date,
        kind=kind,
        text=text,
        file_name=file_name,
        mime_type=mime_type,
        raw=message,
    )


async def route(message: IncomingMessage, settings: Settings) -> HandlerResult:
    """Dispatch a message to its handler.

    Authorization is not checked here: input comes only from the owner's own Saved Messages, and
    the client enforces the self-peer filter before routing.
    """
    # The one route the kind alone cannot decide: `#검토` opts a memo into the review loop
    # (increment 4). Dispatched here rather than inside handle_text so the two handlers stay
    # independent of each other — text_handler owns the one-shot path and knows nothing of reviews.
    if message.kind is MessageKind.TEXT and is_review_request(message.text):
        return await handle_review_request(message, settings)

    handler = ROUTING_TABLE.get(message.kind)
    if handler is None:
        return HandlerResult(reply="🤔 지원하지 않는 형식의 메시지입니다.")
    return await handler(message, settings)
