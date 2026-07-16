"""BotWorker tests: the seam between Qt's thread and the bot's asyncio loop.

Nothing here starts a real client — only the thing the worker does *around* one.
"""

from __future__ import annotations

from concurrent.futures import Future

import pytest

pytest.importorskip("PySide6")

import os  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from contextbot.ui.bot_worker import BotWorker  # noqa: E402


@pytest.fixture
def worker(settings):
    w = BotWorker(settings)
    yield w
    w._loop.close()


def test_health_is_checked_once_the_starting_sequence_finishes(worker, monkeypatch):
    """The owner's bug: Start showed red for 15 seconds and then went green on its own.

    `start_bot` returns immediately — it only queues `start()` on the loop — so the health check
    fired from the click ran against a client that had not connected, and reported
    `telethon-auth: not logged in (run login.py)`. It stayed red until the poll timer happened to
    re-check. This is the first moment a check can tell the truth, so it is the moment to ask.
    """
    asked: list[int] = []
    monkeypatch.setattr(worker, "request_health", lambda: asked.append(1))
    done: Future = Future()
    done.set_result(None)

    worker._after_start(done)

    assert asked == [1]


def test_a_failed_start_is_reported_and_not_health_checked(worker, monkeypatch):
    """A start that raised has nothing to check: the client is not up, and the error is the news."""
    asked: list[int] = []
    errors: list[str] = []
    monkeypatch.setattr(worker, "request_health", lambda: asked.append(1))
    worker.error.connect(errors.append)
    failed: Future = Future()
    failed.set_exception(RuntimeError("Telethon session is not authorized; run login.py"))

    worker._after_start(failed)

    assert asked == []
    assert errors == ["Telethon session is not authorized; run login.py"]
