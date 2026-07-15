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
from .health import HealthChecker, HealthReport
from .hwm import HighWaterMark
from .notifier import Notifier
from .router import build_incoming_message, route
from .security import is_saved_messages
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
    ):
        self._settings = settings
        self._status = status_model
        self._client = client
        self._bot = bot
        self._hwm = hwm or HighWaterMark(settings.session_path.parent / "hwm.json")
        self._notifier: Optional[Notifier] = None
        self._me_id: Optional[int] = None

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

            # First run: baseline to the latest id so existing history isn't imported.
            if not self._hwm.exists():
                latest = await self._latest_saved_id()
                self._hwm.baseline(latest)
                logger.info("First run: baselined HWM to %s (existing history skipped)", latest)

            await self.catch_up()
        except Exception as exc:
            logger.exception("Failed to start client")
            if self._status.status != BotStatus.ERROR:
                self._status.set(BotStatus.ERROR, f"시작 실패: {exc}")
            raise
        self._status.set(BotStatus.RUNNING, "실행 중")
        logger.info("Client started (Saved Messages input, bot replies)")

    async def stop(self) -> None:
        client = self._client
        if client is None:
            self._status.set(BotStatus.STOPPED, "중지됨")
            return
        try:
            if client.is_connected():
                await client.disconnect()
        finally:
            self._status.set(BotStatus.STOPPED, "중지됨")
            logger.info("Client stopped")

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
        """Process Saved Messages with id greater than the HWM, oldest-first. Returns the count."""
        last = self._hwm.value
        count = 0
        async for message in self._client.iter_messages(SAVED, min_id=last, reverse=True):
            await self._process(message, notify_status=False)
            count += 1
        if count:
            logger.info("Catch-up processed %d message(s) since last run", count)
        return count

    async def _on_new_message(self, event) -> None:
        message = getattr(event, "message", None)
        if getattr(event, "chat_id", None) != self._me_id:
            return  # only the owner's Saved Messages
        await self._process(message, notify_status=True)

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
            self._hwm.advance(incoming.message_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error while processing message id=%s", incoming.message_id)
            self._status.set(BotStatus.ERROR, f"처리 오류: {exc}")
            if self._notifier is not None:
                await self._notifier.send(f"⚠️ 처리 중 오류가 발생했습니다: {exc}")
        finally:
            if notify_status and self._status.status == BotStatus.PROCESSING:
                self._status.set(BotStatus.RUNNING, "실행 중")
