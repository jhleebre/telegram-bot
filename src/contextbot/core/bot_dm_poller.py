"""Bot-DM polling, active whenever the app is up.

This sits **beside** :class:`~contextbot.core.notifier.Notifier` rather than inside it, and that
split is the point: capture never depends on the Bot API's 24h update retention, because capture
reads Saved Messages over Telethon. Only the *conversation* polls, and the channel separation is
what stops a reply from being re-ingested as a new capture (docs/PHASE2.md, "Human-in-the-loop via
session preservation").

**It used to run only while a review was open**, which made talking to the bot at any other time a
silent no-op — the message sat in Telegram's queue and was discarded when a review next opened. The
discarding was right; the silence was not, because it left the owner unable to tell a stopped bot
from a broken one. Polling whenever the app is up is what lets **every** message get an answer, and
that is what makes silence mean something precise: *nothing is running*. **Telegram has no
offline autoresponder to borrow** — a bot is a token plus your code, so when the code is down
nothing answers — and a rule with no exceptions is the closest honest substitute. It is safe for the
reason it always was: the bot DM is not the capture channel, so the 24h limit cannot cost a message.
The cost is one idle long-poll request per interval while the window is open.

No new dependency: python-telegram-bot's ``Bot.get_updates(offset/timeout/allowed_updates)`` is on
the ``Bot`` the client already builds. No ``Application``, no ``Updater``.

**Two filters, and the second no longer discards.** Long polling hands back whatever Telegram
retained, so a message is an *answer to a review* only when it is both from the owner's own DM and
**sent after that review began**. Without the second test, a message the owner typed at the bot
*before* the review — sitting in the 24h backlog, or arriving from a stale offset after a crash —
would be read as their answer to a question they had not yet been asked.

That test now **reports rather than drops**: the owner's message is always handed on, with a flag
saying whether it may be *applied*. Dropping it here would have rebuilt the same silence one layer
down — app off, owner sends a message, app starts and opens a review during catch-up, and the
message they sent hours earlier vanishes without a word.

The cutoff is the **review's** start, not the poller's. Those differ exactly when it matters: after
a restart, a reply the owner sent while the app was closed is still a valid answer to a review that
began before it, and keying off this process's start time would silently discard it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol

logger = logging.getLogger("contextbot.bot_dm_poller")

# Long-poll timeout. Telegram holds the request open this long when nothing arrives, so the loop
# costs one idle request per interval — and a review's on_tick (expiry) runs at the same cadence.
_POLL_TIMEOUT_SEC = 25
# Ceiling on how long a get_updates call may hang before we give up on it and retry.
_REQUEST_TIMEOUT_SEC = _POLL_TIMEOUT_SEC + 15
# Backoff after a transport error, so a network blip cannot become a hot loop.
_ERROR_BACKOFF_SEC = 5.0


class PollingBotLike(Protocol):
    async def get_updates(self, **kwargs: Any): ...


class BotDmPoller:
    """Reads the owner's bot-DM messages while the app is running.

    Lifecycle is driven by the client: :meth:`start` once it is up, :meth:`stop` on shutdown. Both
    are idempotent, so re-syncing mid-poll is a no-op.

    ``on_message`` is handed ``(text, answerable)``. ``answerable`` is False when the message
    predates the open review and so cannot be that review's answer — but it is still the owner
    talking, and it still gets a reply.
    """

    def __init__(
        self,
        bot: PollingBotLike,
        owner_chat_id: Optional[int],
        *,
        on_message: Callable[[str, bool], Awaitable[None]],
        on_tick: Optional[Callable[[], Awaitable[None]]] = None,
        get_offset: Callable[[], int],
        set_offset: Callable[[int], None],
        is_active: Callable[[], bool],
        review_started_at: Callable[[], Optional[datetime]],
    ) -> None:
        self._bot = bot
        self._owner_chat_id = owner_chat_id
        self._on_message = on_message
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
        """Begin polling if we are not already."""
        if self.is_running or self._owner_chat_id is None:
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("bot-DM polling started")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown must not raise
            pass
        logger.info("bot-DM polling stopped")

    async def _loop(self) -> None:
        try:
            while self._is_active():
                if self._on_tick is not None:
                    await self._on_tick()
                    if not self._is_active():
                        break  # went down during the tick; do not open a 25s long-poll on the way out
                try:
                    await self._poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - a transport error must not kill the loop
                    logger.exception("get_updates failed; retrying")
                    await asyncio.sleep(_ERROR_BACKOFF_SEC)
        except asyncio.CancelledError:
            pass
        finally:
            logger.debug("bot-DM poll loop exited")

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
                # forever, and the message is the owner's to re-send.
                self._set_offset(update_id + 1)
            found = self._owner_message(update)
            if found is None:
                continue
            text, answerable = found
            await self._on_message(text, answerable)
            if not self._is_active():
                return  # we went down mid-batch; stop reading

    def _owner_message(self, update: Any) -> tuple[str, bool] | None:
        """``(text, answerable)`` for an owner's DM, or None when it is not one.

        Only two things make an update *not ours*: it carries no text, or it is not from the owner's
        chat. Everything else the owner typed is handed on — see the module docstring for why the
        backlog filter reports rather than discarding.
        """
        message = getattr(update, "message", None)
        text = getattr(message, "text", None) if message is not None else None
        if not text:
            return None

        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        if chat_id != self._owner_chat_id:
            logger.debug("ignoring update from chat %s", chat_id)
            return None

        return text, self._postdates_review(message)

    def _postdates_review(self, message: Any) -> bool:
        """False when this message was sent before the open review was ever asked about.

        True when there is no review at all: with nothing to answer, there is nothing to answer
        *late*, and the client's status reply is the right thing to give it.
        """
        started = self._review_started_at()
        if started is None:
            return True
        sent = getattr(message, "date", None)
        if not isinstance(sent, datetime):
            return True
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=timezone.utc)
        if sent >= started:
            return True
        logger.info("bot-DM message predates the open review; not treating it as an answer")
        return False
