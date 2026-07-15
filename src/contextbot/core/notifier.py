"""Send-only bot notifier: posts replies/confirmations to the owner's bot DM.

Kept deliberately minimal — it never polls, so the Bot API's 24h update-retention limit never
affects message capture (capture happens over Saved Messages via Telethon). Replies go to the bot
DM, a separate channel from Saved Messages, so they are never re-ingested as new input.
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol

logger = logging.getLogger("contextbot.notifier")


class BotLike(Protocol):
    async def get_me(self): ...
    async def send_message(self, chat_id: int, text: str): ...


class Notifier:
    def __init__(self, bot: BotLike, owner_chat_id: Optional[int] = None):
        self._bot = bot
        self._owner_chat_id = owner_chat_id

    def set_owner_chat_id(self, chat_id: int) -> None:
        self._owner_chat_id = chat_id

    @property
    def owner_chat_id(self) -> Optional[int]:
        return self._owner_chat_id

    async def send(self, text: str) -> bool:
        """Send ``text`` to the owner. Returns False (and logs) if not deliverable."""
        if self._owner_chat_id is None:
            logger.warning("Notifier has no owner_chat_id; dropping message: %s", text)
            return False
        try:
            await self._bot.send_message(self._owner_chat_id, text)
            return True
        except Exception:  # noqa: BLE001 - never let a failed reply break processing
            logger.exception("Failed to send reply to owner")
            return False
