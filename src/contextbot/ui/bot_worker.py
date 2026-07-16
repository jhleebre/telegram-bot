"""Qt worker thread that hosts the asyncio event loop running the bot.

Qt owns the main thread; the bot's asyncio loop runs here. The two communicate via Qt signals
(status/log/health) and ``asyncio.run_coroutine_threadsafe`` for control commands. Because the
loop lives entirely inside this thread, closing the app tears it down — the bot never runs in the
background.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from PySide6.QtCore import QThread, Signal

from ..config import Settings
from ..core.client_service import ClientService
from ..core.status import BotStatus, StatusModel


class BotWorker(QThread):
    status_changed = Signal(str, str)   # BotStatus value, message
    log_line = Signal(str)
    # (summary, detail): the collapsed window shows only the summary, so the report has to
    # arrive already reduced to one line — the UI must not re-derive it from the detail text.
    health_ready = Signal(str, str)
    error = Signal(str)

    def __init__(self, settings: Settings):
        super().__init__()
        self._settings = settings
        self._loop = asyncio.new_event_loop()
        self._status_model = StatusModel()
        self._status_model.subscribe(self._on_status)
        self._service = ClientService(settings, self._status_model)

    # ------------------------------------------------------------- thread body
    def run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    # --------------------------------------------------- status → Qt signal
    def _on_status(self, status: BotStatus, message: str) -> None:
        self.status_changed.emit(status.value, message)

    # ---------------------------------------------- control (main thread → loop)
    def start_bot(self) -> None:
        fut = asyncio.run_coroutine_threadsafe(self._service.start(), self._loop)
        fut.add_done_callback(self._report_exception)

    def stop_bot(self) -> None:
        asyncio.run_coroutine_threadsafe(self._service.stop(), self._loop)

    def request_health(self) -> None:
        fut = asyncio.run_coroutine_threadsafe(self._service.health(), self._loop)

        def _done(f) -> None:
            try:
                report = f.result()
            except Exception as exc:  # noqa: BLE001
                self.health_ready.emit("🔴 확인 실패", f"health check failed: {exc}")
                return
            self.health_ready.emit(report.summary(), report.as_text())

        fut.add_done_callback(_done)

    def _report_exception(self, fut) -> None:
        exc = fut.exception()
        if exc is not None:
            self.error.emit(str(exc))

    # ------------------------------------------------------------- shutdown
    def shutdown(self, timeout: float = 10.0) -> None:
        """Stop the bot and the event loop, then join the thread. Safe to call once."""
        if self._loop.is_running():
            try:
                fut = asyncio.run_coroutine_threadsafe(self._service.stop(), self._loop)
                fut.result(timeout=timeout)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
        self.wait(int(timeout * 1000))
