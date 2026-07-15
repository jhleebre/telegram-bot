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


class DeferMessage(Exception):
    """Raised by a handler when a message cannot be processed *now* but will succeed later.

    The client responds by leaving the high-water-mark unadvanced, stopping, and showing why. The
    owner restarts once the condition clears, and catch-up replays the message from where it left
    off. Deferring is lossless: the message stays in Saved Messages, which is the durable input.

    Raise this **only for transient, time-bound** failures — currently just an exhausted Claude
    usage limit, which resets on a fixed window.

    Do **not** raise it for permanent failures (a corrupt file, a missing CLI, a bug in our code).
    Those recur on every retry, so halting would stop the bot again on each Start and wedge it
    forever. Let those surface as ordinary exceptions: the client skips the message and reports it.

    Handlers must raise this **before** any side effect (writing a note, moving or deleting the
    original), because the replay re-runs the handler from the start.
    """


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
