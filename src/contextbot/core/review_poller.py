"""Bot-DM polling, active only while a review is open.

This sits **beside** :class:`~contextbot.core.notifier.Notifier` rather than inside it, and that
split is the point: capture never depends on the Bot API's 24h update retention, because capture
reads Saved Messages over Telethon. Only the review conversation polls, only while a review is
waiting, and the channel separation is what stops a review reply from being re-ingested as a new
capture (docs/PHASE2.md, "Human-in-the-loop via session preservation").

No new dependency: python-telegram-bot's ``Bot.get_updates(offset/timeout/allowed_updates)`` is on
the ``Bot`` the client already builds. No ``Application``, no ``Updater``.

**Two filters, not one, and the second is the one that matters.** Long polling hands back whatever
Telegram retained, so a reply is accepted only when it is both:

1. from the owner's own DM, and
2. **sent after the review began**.

Without (2), a message the owner typed at the bot *before* the review — sitting in Telegram's
24h backlog, or arriving from a stale offset after a crash — would be read as their answer to a
question they had not yet been asked. The offset makes that unlikely; the timestamp makes it
impossible.

The cutoff is the **review's** start, not the poller's. Those differ exactly when it matters: after
a restart, a reply the owner sent while the app was closed is still a valid answer to a review that
began before it, and keying off this process's start time would silently discard it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol

logger = logging.getLogger("contextbot.review_poller")

# Long-poll timeout. Telegram holds the request open this long when nothing arrives, so the loop
# costs one idle request per interval — and a review's on_tick (expiry) runs at the same cadence.
_POLL_TIMEOUT_SEC = 25
# Ceiling on how long a get_updates call may hang before we give up on it and retry.
_REQUEST_TIMEOUT_SEC = _POLL_TIMEOUT_SEC + 15
# Backoff after a transport error, so a network blip cannot become a hot loop.
_ERROR_BACKOFF_SEC = 5.0


class PollingBotLike(Protocol):
    async def get_updates(self, **kwargs: Any): ...


class ReviewPoller:
    """Reads the owner's bot-DM replies while a review is open.

    Lifecycle is driven by the client: :meth:`start` when a review exists, :meth:`stop` on
    shutdown. Both are idempotent, so a second review starting mid-poll is a no-op.
    """

    def __init__(
        self,
        bot: PollingBotLike,
        owner_chat_id: Optional[int],
        *,
        on_reply: Callable[[str], Awaitable[None]],
        on_tick: Optional[Callable[[], Awaitable[None]]] = None,
        get_offset: Callable[[], int],
        set_offset: Callable[[int], None],
        is_active: Callable[[], bool],
        review_started_at: Callable[[], Optional[datetime]],
    ) -> None:
        self._bot = bot
        self._owner_chat_id = owner_chat_id
        self._on_reply = on_reply
        self._on_tick = on_tick
        self._get_offset = get_offset
        self._set_offset = set_offset
        self._is_active = is_active
        self._review_started_at = review_started_at
        self._task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Begin polling if a review is open and we are not already polling."""
        if self.is_running or self._owner_chat_id is None:
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("review polling started")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown must not raise
            pass
        logger.info("review polling stopped")

    async def _loop(self) -> None:
        try:
            while self._is_active():
                if self._on_tick is not None:
                    await self._on_tick()
                    if not self._is_active():  # the tick may have ended the review (expiry)
                        break
                try:
                    await self._poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - a transport error must not kill the review
                    logger.exception("get_updates failed; retrying")
                    await asyncio.sleep(_ERROR_BACKOFF_SEC)
        except asyncio.CancelledError:
            pass
        finally:
            logger.debug("review poll loop exited")

    async def _poll_once(self) -> None:
        updates = await self._bot.get_updates(
            offset=self._get_offset() or None,
            timeout=_POLL_TIMEOUT_SEC,
            allowed_updates=["message"],
            read_timeout=_REQUEST_TIMEOUT_SEC,
        )
        for update in updates or []:
            update_id = getattr(update, "update_id", None)
            if update_id is not None:
                # Advance past it first: an update that makes handling raise must not be replayed
                # forever, and the reply is the owner's to re-send.
                self._set_offset(update_id + 1)
            text = self._owner_text(update)
            if text is None:
                continue
            await self._on_reply(text)
            if not self._is_active():
                return  # the review ended on that reply; stop reading its channel

    def _owner_text(self, update: Any) -> str | None:
        """The reply text, if this update is one the open review may act on."""
        message = getattr(update, "message", None)
        text = getattr(message, "text", None) if message is not None else None
        if not text:
            return None

        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        if chat_id != self._owner_chat_id:
            logger.debug("ignoring update from chat %s", chat_id)
            return None

        # The backlog filter. Telegram retains updates for 24h, so "it arrived" is not "it answers
        # us" — a message predating the review is the owner talking about something else.
        sent = getattr(message, "date", None)
        started = self._review_started_at()
        if isinstance(sent, datetime) and started is not None:
            if sent.tzinfo is None:
                sent = sent.replace(tzinfo=timezone.utc)
            if sent < started:
                logger.info("ignoring bot-DM message from before the review began")
                return None
        return text
