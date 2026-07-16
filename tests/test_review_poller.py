"""Review poller tests: reading the owner's bot-DM replies, and only those.

The bot is a fake — no network. What matters here is what the poller *refuses* to hand on: polling
returns whatever Telegram retained for up to 24h, so "an update arrived" is a long way from "the
owner answered our question".
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from contextbot.core.review_poller import ReviewPoller

OWNER = 42
NOW = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)
REVIEW_STARTED = NOW - timedelta(minutes=5)


class FakeChat:
    def __init__(self, id):
        self.id = id


class FakeMessage:
    def __init__(self, text, *, chat_id=OWNER, date=NOW):
        self.text = text
        self.chat = FakeChat(chat_id)
        self.date = date


class FakeUpdate:
    def __init__(self, update_id, message):
        self.update_id = update_id
        self.message = message


class FakeBot:
    """Hands back one canned batch per call, then blocks like a real long poll would."""

    def __init__(self, *batches, fail_first: bool = False):
        self._batches = list(batches)
        self._fail_first = fail_first
        self.calls: list[dict] = []

    async def get_updates(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail_first:
            self._fail_first = False
            raise RuntimeError("network unreachable")
        if self._batches:
            return self._batches.pop(0)
        await asyncio.sleep(3600)  # nothing to report: stay open like a long poll
        return []


class Harness:
    """A poller wired to in-memory state, with the review active until told otherwise."""

    def __init__(self, bot, *, started=REVIEW_STARTED, on_tick=None):
        self.replies: list[str] = []
        self.offset = 0
        self.active = True
        self.started = started
        self.poller = ReviewPoller(
            bot,
            OWNER,
            on_reply=self._on_reply,
            on_tick=on_tick,
            get_offset=lambda: self.offset,
            set_offset=self._set_offset,
            is_active=lambda: self.active,
            review_started_at=lambda: self.started,
        )

    async def _on_reply(self, text):
        self.replies.append(text)

    def _set_offset(self, value):
        self.offset = value

    async def run_until(self, predicate, *, timeout=2.0):
        """Start polling and wait for ``predicate`` — never a bare sleep."""
        self.poller.start()
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        await self.poller.stop()
        return predicate()


async def test_an_owner_reply_reaches_the_review():
    bot = FakeBot([FakeUpdate(1, FakeMessage("담당자는 김철수"))])
    h = Harness(bot)

    assert await h.run_until(lambda: h.replies)
    assert h.replies == ["담당자는 김철수"]


async def test_polling_asks_only_for_messages():
    bot = FakeBot([FakeUpdate(1, FakeMessage("확인"))])
    h = Harness(bot)
    await h.run_until(lambda: h.replies)

    assert bot.calls[0]["allowed_updates"] == ["message"]
    assert bot.calls[0]["timeout"] > 0


async def test_a_message_from_another_chat_is_ignored():
    bot = FakeBot([FakeUpdate(1, FakeMessage("확인", chat_id=999))])
    h = Harness(bot)

    assert not await h.run_until(lambda: h.replies, timeout=0.3)


async def test_a_message_sent_before_the_review_began_is_ignored():
    """The filter that the offset alone cannot provide.

    Telegram retains updates for 24h, so a message the owner sent the bot *before* the review — or
    one replayed from a stale offset after a crash — would otherwise be read as their answer to a
    question they had not yet been asked.
    """
    stale = FakeUpdate(1, FakeMessage("확인", date=REVIEW_STARTED - timedelta(hours=2)))
    h = Harness(FakeBot([stale]))

    assert not await h.run_until(lambda: h.replies, timeout=0.3)


async def test_a_reply_sent_while_the_app_was_closed_is_still_accepted():
    """The cutoff is the *review's* start, not the poller's.

    A restart must not silently discard an answer the owner sent while the app was down — the
    review began before it, so it is a valid reply. This is why the poller asks the store when the
    review started rather than stamping its own start time.
    """
    while_closed = FakeUpdate(1, FakeMessage("확인", date=REVIEW_STARTED + timedelta(minutes=1)))
    h = Harness(FakeBot([while_closed]))

    assert await h.run_until(lambda: h.replies)


async def test_a_naive_timestamp_is_treated_as_utc():
    naive = FakeUpdate(1, FakeMessage("확인", date=NOW.replace(tzinfo=None)))
    h = Harness(FakeBot([naive]))

    assert await h.run_until(lambda: h.replies)


async def test_a_non_text_update_is_ignored():
    h = Harness(FakeBot([FakeUpdate(1, FakeMessage(None))]))
    assert not await h.run_until(lambda: h.replies, timeout=0.3)


async def test_an_update_without_a_message_is_ignored():
    h = Harness(FakeBot([FakeUpdate(1, None)]))
    assert not await h.run_until(lambda: h.replies, timeout=0.3)


async def test_the_offset_advances_past_a_handled_update():
    bot = FakeBot([FakeUpdate(77, FakeMessage("확인"))])
    h = Harness(bot)
    await h.run_until(lambda: h.replies)

    assert h.offset == 78


async def test_the_offset_advances_past_an_ignored_update_too():
    """An update we skip must not be re-read forever."""
    bot = FakeBot([FakeUpdate(77, FakeMessage("확인", chat_id=999))])
    h = Harness(bot)
    await h.run_until(lambda: h.offset == 78, timeout=0.5)

    assert h.offset == 78


async def test_a_stored_offset_is_sent_on_the_first_poll():
    bot = FakeBot([FakeUpdate(101, FakeMessage("확인"))])
    h = Harness(bot)
    h.offset = 100
    await h.run_until(lambda: h.replies)

    assert bot.calls[0]["offset"] == 100


async def test_polling_stops_when_the_review_ends():
    """The poller exists only while a review is open — that is what keeps capture independent of
    the Bot API's 24h retention limit."""
    h = Harness(FakeBot([FakeUpdate(1, FakeMessage("확인"))]))

    async def end_review(text):
        h.replies.append(text)
        h.active = False

    h.poller._on_reply = end_review
    h.poller.start()
    for _ in range(100):
        if not h.poller.is_running:
            break
        await asyncio.sleep(0.01)

    assert not h.poller.is_running


async def test_a_transport_error_does_not_kill_the_loop():
    """A network blip must not silently end a review — the owner would never know."""
    bot = FakeBot([FakeUpdate(1, FakeMessage("확인"))], fail_first=True)
    h = Harness(bot)

    assert await h.run_until(lambda: h.replies, timeout=8.0)


async def test_the_tick_runs_each_interval():
    ticks = []

    async def on_tick():
        ticks.append(1)

    h = Harness(FakeBot([FakeUpdate(1, FakeMessage("확인"))]), on_tick=on_tick)
    await h.run_until(lambda: h.replies)

    assert ticks


async def test_a_tick_that_ends_the_review_stops_the_poll():
    """Expiry runs on the tick; when it retires the review, there is nothing left to poll for."""
    async def on_tick():
        h.active = False

    h = Harness(FakeBot(), on_tick=on_tick)
    h.poller.start()
    for _ in range(100):
        if not h.poller.is_running:
            break
        await asyncio.sleep(0.01)

    assert not h.poller.is_running


async def test_start_is_idempotent():
    h = Harness(FakeBot())
    h.poller.start()
    task = h.poller._task
    h.poller.start()

    assert h.poller._task is task
    await h.poller.stop()


async def test_stop_is_safe_before_start():
    await Harness(FakeBot()).poller.stop()


async def test_no_owner_chat_id_means_no_polling():
    """Without a target there is nobody to read, and get_updates would be pure waste."""
    bot = FakeBot()
    poller = ReviewPoller(
        bot, None,
        on_reply=lambda text: None,
        get_offset=lambda: 0,
        set_offset=lambda v: None,
        is_active=lambda: True,
        review_started_at=lambda: None,
    )
    poller.start()

    assert not poller.is_running
    assert bot.calls == []
