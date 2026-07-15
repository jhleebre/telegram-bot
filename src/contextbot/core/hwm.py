"""High-water-mark store: the id of the last processed Saved Messages message.

Persisted to a small JSON file so the app knows where to resume after being closed. The mark is
advanced only after a message is successfully processed, so an interrupted run re-processes the
tail rather than skipping it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger("contextbot.hwm")

_KEY = "saved_messages_last_id"


class HighWaterMark:
    def __init__(self, path: Path):
        self._path = path
        self._last_id: int = 0
        self._loaded = False

    # ------------------------------------------------------------------ load/save
    def load(self) -> int:
        """Load the stored mark (0 if none / unreadable)."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._last_id = int(data.get(_KEY, 0))
        except (FileNotFoundError, ValueError, OSError):
            self._last_id = 0
        self._loaded = True
        return self._last_id

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({_KEY: self._last_id}, fh)
            os.replace(tmp, self._path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # --------------------------------------------------------------------- access
    @property
    def value(self) -> int:
        if not self._loaded:
            self.load()
        return self._last_id

    def exists(self) -> bool:
        return self._path.exists()

    def advance(self, message_id: int) -> None:
        """Move the mark forward to ``message_id`` (never backward) and persist."""
        if not self._loaded:
            self.load()
        if message_id > self._last_id:
            self._last_id = message_id
            self._save()

    def baseline(self, latest_id: int) -> None:
        """Set the mark to ``latest_id`` unconditionally (first-run initialization).

        Used so that existing Saved Messages history is not retroactively turned into notes.
        """
        self._last_id = latest_id
        self._loaded = True
        self._save()
        logger.info("HWM baselined to latest id=%s", latest_id)
