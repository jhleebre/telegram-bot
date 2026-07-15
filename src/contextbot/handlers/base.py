"""Handler abstractions shared by all message handlers.

Handlers operate on a lightweight :class:`IncomingMessage` (decoupled from python-telegram-bot's
``Update``) so they can be unit-tested without constructing real Telegram objects. The router is
responsible for building :class:`IncomingMessage` from an ``Update`` and for sending the reply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Protocol


class MessageKind(str, Enum):
    TEXT = "text"
    AUDIO = "audio"
    IMAGE = "image"
    DOCUMENT = "document"
    MARKDOWN = "markdown"
    UNKNOWN = "unknown"


@dataclass
class IncomingMessage:
    """A normalized inbound message the handlers understand."""

    user_id: Optional[int]
    chat_id: int
    message_id: int
    date: datetime
    kind: MessageKind
    text: str = ""
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    # The underlying Telethon message, kept for Phase 2 media downloads (download_media).
    # Unused in Phase 1; excluded from equality/repr to keep the dataclass value-like.
    raw: Optional[object] = field(default=None, compare=False, repr=False)


@dataclass
class HandlerResult:
    """Outcome of handling a message."""

    reply: str
    saved_path: Optional[Path] = None
    extra_paths: list[Path] = field(default_factory=list)


class Handler(Protocol):
    """Async callable that turns an :class:`IncomingMessage` into a :class:`HandlerResult`."""

    async def __call__(self, message: IncomingMessage, settings) -> HandlerResult: ...
