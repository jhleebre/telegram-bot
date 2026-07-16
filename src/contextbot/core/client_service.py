"""Client service: Telethon user client (input) + send-only bot (reply).

Input comes from the owner's **Saved Messages** (read via the Telethon user client), which is
durable and history-readable, so messages sent while the app was closed are processed on the next
Start. Replies/confirmations go to the owner's **bot DM** via a send-only bot — a separate channel,
so they are never re-ingested.

Message processing (:meth:`_process`) is decoupled from Telegram types (duck-typed) and the client
is injectable, so the catch-up/dispatch logic is unit-testable with fakes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from telethon import TelegramClient, events

from ..config import Settings
from ..handlers.base import DeferMessage
from ..handlers.conversation import discard_incomplete, expire_stale, handle_reply
from .health import HealthChecker, HealthReport
from .hwm import HighWaterMark
from .notifier import Notifier
from .review_poller import ReviewPoller
from .router import build_incoming_message, route
from .security import is_saved_messages
from .session_store import build_store
from .status import BotStatus, StatusModel

logger = logging.getLogger("contextbot.client")

SAVED = "me"  # Telethon shortcut for the Saved Messages chat


class ClientService:
    def __init__(
        self,
        settings: Settings,
        status_model: StatusModel,
        *,
        client=None,
        bot=None,
        hwm: Optional[HighWaterMark] = None,
        store=None,
    ):
        self._settings = settings
        self._status = status_model
        self._client = client
        self._bot = bot
        self._hwm = hwm or HighWaterMark(settings.session_path.parent / "hwm.json")
        self._notifier: Optional[Notifier] = None
        self._me_id: Optional[int] = None
        # The review store is the same file the handlers write through (it is the authority, not a
        # cache), so this instance agreeing with theirs costs nothing to arrange.
        self._store = store or build_store(settings)
        self._poller: Optional[ReviewPoller] = None

    # ------------------------------------------------------------------ builders
    def _build_client(self) -> TelegramClient:
        self._settings.session_path.parent.mkdir(parents=True, exist_ok=True)
        return TelegramClient(
            str(self._settings.session_path),
            self._settings.api_id,
            self._settings.api_hash,
        )

    def _build_bot(self):
        from telegram import Bot

        return Bot(self._settings.telegram_bot_token)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._status.set(BotStatus.STARTING, "연결 중…")
        try:
            self._client = self._client or self._build_client()
            await self._client.connect()
            if not await self._client.is_user_authorized():
                self._status.set(BotStatus.ERROR, "로그인 필요 — login.py 를 실행하세요")
                raise RuntimeError("Telethon session is not authorized; run login.py")

            me = await self._client.get_me()
            self._me_id = getattr(me, "id", None)

            self._bot = self._bot or self._build_bot()
            owner_chat_id = self._settings.owner_chat_id or self._me_id
            self._notifier = Notifier(self._bot, owner_chat_id)

            # Register the live handler before catch-up so nothing sent during startup is missed;
            # the HWM guard in _process dedupes any overlap.
            self._client.add_event_handler(self._on_new_message, events.NewMessage())

            # Before catch-up: clear a review whose first turn died with the app. It holds the
            # one-at-a-time guard against the very replay catch-up is about to perform.
            discard_incomplete(self._store)

            # First run: baseline to the latest id so existing history isn't imported.
            if not self._hwm.exists():
                latest = await self._latest_saved_id()
                self._hwm.baseline(latest)
                logger.info("First run: baselined HWM to %s (existing history skipped)", latest)

            await self.catch_up()
            # A review survives a restart — the transcript is on disk and keyed by a directory we
            # kept (the measured resume contract). So if one was open when the app closed, pick it
            # back up rather than stranding the owner's draft.
            self._sync_polling()
        except DeferMessage as exc:
            # The backlog hit a usage limit. Halt instead of falling through to RUNNING — the
            # deferred message and everything after it stay unprocessed for the next Start.
            await self._halt(exc)
            return
        except Exception as exc:
            logger.exception("Failed to start client")
            if self._status.status != BotStatus.ERROR:
                self._status.set(BotStatus.ERROR, f"시작 실패: {exc}")
            raise
        self._status.set(BotStatus.RUNNING, "실행 중")
        logger.info("Client started (Saved Messages input, bot replies)")

    async def stop(self, *, message: str = "중지됨") -> None:
        """Disconnect and report STOPPED. ``message`` lets a self-initiated stop explain itself."""
        if self._poller is not None:
            await self._poller.stop()
        client = self._client
        if client is None:
            self._status.set(BotStatus.STOPPED, message)
            return
        try:
            if client.is_connected():
                await client.disconnect()
        finally:
            self._status.set(BotStatus.STOPPED, message)
            logger.info("Client stopped")

    async def _halt(self, exc: DeferMessage) -> None:
        """Stop reading because a message was deferred; the next Start replays it.

        The DM goes out **before** disconnecting: the bot replies over the Bot API, not the
        Telethon client, so it still works — and if the disconnect interrupts us, the owner has
        already been told why the bot went quiet.
        """
        logger.warning("Halting: %s", exc)
        if self._notifier is not None:
            await self._notifier.send(
                "⏸ 사용량 한도에 도달해 봇을 멈췄습니다.\n"
                "메시지는 그대로 남아 있고, 한도 리셋 후 Start하면 이어서 처리합니다.\n"
                f"({exc})"
            )
        await self.stop(message="⏸ 사용량 한도 — 리셋 후 Start를 눌러주세요")

    # ------------------------------------------------------------------- reviews
    def _sync_polling(self) -> None:
        """Poll the bot DM exactly while a review is waiting on the owner.

        Kept out of :class:`Notifier`, which stays send-only: capture reads Saved Messages over
        Telethon and must never depend on the Bot API's 24h update retention. Only the review
        conversation polls, and only for as long as one is open.
        """
        if not self._store.has_pending():
            return
        if self._poller is None:
            self._poller = ReviewPoller(
                self._bot,
                self._notifier.owner_chat_id if self._notifier else None,
                on_reply=self._on_review_reply,
                on_tick=self._on_review_tick,
                get_offset=lambda: self._store.update_offset,
                set_offset=self._store.set_update_offset,
                is_active=self._store.has_pending,
                review_started_at=self._review_started_at,
            )
        self._poller.start()

    def _review_started_at(self):
        review = self._store.pending()
        return review.created_at if review is not None else None

    async def _on_review_reply(self, text: str) -> None:
        """Feed one bot-DM reply into the open review and relay the outcome.

        A review turn never raises :class:`DeferMessage` — see docs/PHASE2.md. There is no handler
        to replay, and halting would stop this very poller, so a usage limit here reports itself
        and leaves the review open for the owner to retry.
        """
        was_running = self._status.status == BotStatus.RUNNING
        if was_running:
            self._status.set(BotStatus.PROCESSING, "검토 반영 중…")
        try:
            reply = await handle_reply(text, self._settings, store=self._store)
        except Exception as exc:  # noqa: BLE001 - a bad turn must not kill the poll loop
            logger.exception("Review turn failed")
            reply = f"⚠️ 검토 처리 중 오류가 발생했습니다: {exc}"
        finally:
            if was_running and self._status.status == BotStatus.PROCESSING:
                self._status.set(BotStatus.RUNNING, "실행 중")
        if reply and self._notifier is not None:
            await self._notifier.send(reply)

    async def _on_review_tick(self) -> None:
        """Runs once per poll interval: retire a review the owner never answered."""
        try:
            reply = await expire_stale(self._settings, store=self._store)
        except Exception:  # noqa: BLE001
            logger.exception("Review expiry check failed")
            return
        if reply and self._notifier is not None:
            await self._notifier.send(reply)

    def is_connected(self) -> bool:
        try:
            return bool(self._client and self._client.is_connected())
        except Exception:  # noqa: BLE001
            return False

    # -------------------------------------------------------------------- health
    async def health(self) -> HealthReport:
        client = self._client or self._build_client()
        bot = self._bot or self._build_bot()
        return await HealthChecker(client, bot, self._settings).check()

    # -------------------------------------------------------------- input reading
    async def _latest_saved_id(self) -> int:
        async for message in self._client.iter_messages(SAVED, limit=1):
            return getattr(message, "id", 0)
        return 0

    async def catch_up(self) -> int:
        """Process Saved Messages with id greater than the HWM, oldest-first. Returns the count.

        A :class:`DeferMessage` breaks the loop and propagates. That is essential, not incidental:
        the HWM is a *single* watermark, so continuing past a deferred message and advancing it on
        a later success would put the deferred id below the watermark and strand it forever.
        Stopping at the first deferral is what keeps the replay correct and ordered.
        """
        last = self._hwm.value
        count = 0
        try:
            async for message in self._client.iter_messages(SAVED, min_id=last, reverse=True):
                await self._process(message, notify_status=False)
                count += 1
        finally:
            if count:
                logger.info("Catch-up processed %d message(s) since last run", count)
        return count

    async def _on_new_message(self, event) -> None:
        message = getattr(event, "message", None)
        if getattr(event, "chat_id", None) != self._me_id:
            return  # only the owner's Saved Messages
        try:
            await self._process(message, notify_status=True)
        except DeferMessage as exc:
            await self._halt(exc)

    # ------------------------------------------------------------------ dispatch
    async def _process(self, message, *, notify_status: bool) -> None:
        incoming = build_incoming_message(message)
        if incoming is None or incoming.kind is None:
            return
        # Defensive self-peer filter (live events already filtered; catch-up reads only SAVED).
        if incoming.chat_id and self._me_id and not is_saved_messages(incoming.chat_id, self._me_id):
            return
        # Dedupe overlap between catch-up and live events.
        if incoming.message_id <= self._hwm.value:
            return

        if notify_status:
            self._status.set(BotStatus.PROCESSING, "메시지 처리 중…")
        try:
            result = await route(incoming, self._settings)
            if result.saved_path is not None:
                logger.info("Saved note: %s", result.saved_path)
            if result.reply and self._notifier is not None:
                await self._notifier.send(result.reply)
            # The HWM means *ingested*, not *note written*. A job that opened a review is fully
            # ingested — its draft and resume handle are on disk, and the store owns it from here.
            # Holding the watermark instead would block every later capture behind an unanswered
            # review, and make a restart re-run the whole job over a draft that already exists.
            self._hwm.advance(incoming.message_id)
            # Ask the store, not the result: a review is durable state, so the store is the truth
            # about whether one is open, and the handlers need no new channel to say so.
            self._sync_polling()
        except DeferMessage:
            # Transient (usage limit): leave the HWM untouched so the next Start replays this
            # message, and let the caller halt. Never advance here.
            raise
        except Exception as exc:  # noqa: BLE001
            # Permanent (corrupt file, a bug): retrying would fail identically and halting would
            # wedge the bot on every Start, so skip past it — but advance the HWM *deliberately*
            # and say so, because a single watermark means the message is not retried again.
            logger.exception("Error while processing message id=%s — skipping", incoming.message_id)
            self._hwm.advance(incoming.message_id)
            if self._notifier is not None:
                await self._notifier.send(
                    f"⚠️ 메시지 처리 실패 (id={incoming.message_id})\n"
                    f"원인: {exc}\n"
                    "이 메시지는 건너뜁니다 — 자동 재시도하지 않습니다.\n"
                    "(Saved Messages에 원본이 남아 있으니 필요하면 다시 보내주세요)"
                )
        finally:
            if notify_status and self._status.status == BotStatus.PROCESSING:
                self._status.set(BotStatus.RUNNING, "실행 중")
