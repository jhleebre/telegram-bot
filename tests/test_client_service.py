from dataclasses import dataclass

from contextbot.core.client_service import ClientService
from contextbot.core.hwm import HighWaterMark
from contextbot.core.status import BotStatus, StatusModel

from .conftest import (
    OWNER_ID,
    FakeBot,
    FakeClient,
    text_message,
    voice_message,
)


@dataclass
class FakeEvent:
    message: object
    chat_id: int


def _make(settings, messages, tmp_path, *, hwm_start=None, me_id=OWNER_ID):
    hwm = HighWaterMark(tmp_path / "hwm.json")
    if hwm_start is not None:
        hwm.baseline(hwm_start)  # creates the file so start() won't re-baseline
    client = FakeClient(messages, me_id=me_id)
    bot = FakeBot()
    svc = ClientService(settings, StatusModel(), client=client, bot=bot, hwm=hwm)
    return svc, client, bot, hwm


async def test_catchup_processes_backlog_in_order(settings, tmp_path):
    msgs = [text_message(2, "둘째"), text_message(1, "첫째")]
    svc, client, bot, hwm = _make(settings, msgs, tmp_path, hwm_start=0)

    await svc.start()

    notes = sorted(settings.inbox_dir.iterdir())
    assert len(notes) == 2
    assert len(bot.sent) == 2  # a confirmation per note
    assert hwm.value == 2
    assert svc._status.status == BotStatus.RUNNING


async def test_first_run_baselines_and_skips_history(settings, tmp_path):
    msgs = [text_message(1, "old"), text_message(2, "older"), text_message(3, "newest")]
    svc, client, bot, hwm = _make(settings, msgs, tmp_path, hwm_start=None)

    await svc.start()

    # No notes created; HWM jumped to the latest existing id.
    assert not list(settings.inbox_dir.iterdir())
    assert hwm.value == 3


async def test_only_messages_after_hwm_processed(settings, tmp_path):
    msgs = [text_message(5, "before"), text_message(6, "after")]
    svc, client, bot, hwm = _make(settings, msgs, tmp_path, hwm_start=5)

    await svc.start()

    notes = list(settings.inbox_dir.iterdir())
    assert len(notes) == 1  # only id=6
    assert hwm.value == 6


async def test_audio_is_transcribed_and_advances(settings, tmp_path, fake_stt):
    """Audio flows through the real pipeline now. The engine is off in the fixture, so this lands
    on the transcript note — but the HWM still advances, because the message *was* ingested."""
    svc, client, bot, hwm = _make(settings, [voice_message(7)], tmp_path, hwm_start=0)

    await svc.start()

    assert len(list(settings.inbox_dir.iterdir())) == 1
    # [0] is the "받았습니다" ack that precedes the slow work; the outcome is [1].
    assert "저장됨" in bot.sent[-1][1]
    assert hwm.value == 7


async def test_live_event_processes_and_dedupes(settings, tmp_path):
    svc, client, bot, hwm = _make(settings, [], tmp_path, hwm_start=0)
    await svc.start()

    # A new live message is processed.
    await svc._on_new_message(FakeEvent(text_message(1, "live"), chat_id=OWNER_ID))
    assert len(list(settings.inbox_dir.iterdir())) == 1
    assert hwm.value == 1

    # The same message id again is deduped (no second note).
    await svc._on_new_message(FakeEvent(text_message(1, "live"), chat_id=OWNER_ID))
    assert len(list(settings.inbox_dir.iterdir())) == 1


async def test_live_event_ignores_other_chats(settings, tmp_path):
    svc, client, bot, hwm = _make(settings, [], tmp_path, hwm_start=0)
    await svc.start()

    # A message from a different chat (not Saved Messages) is ignored.
    await svc._on_new_message(FakeEvent(text_message(50, "stranger"), chat_id=OWNER_ID + 1))
    assert not list(settings.inbox_dir.iterdir())
    assert hwm.value == 0


async def test_confirmation_sent_to_owner(settings, tmp_path):
    svc, client, bot, hwm = _make(settings, [text_message(1, "메모")], tmp_path, hwm_start=0)
    await svc.start()
    assert bot.sent
    chat_id, text = bot.sent[0]
    assert chat_id == OWNER_ID  # derived from get_me()
    assert "저장됨" in text


# ------------------------------------------------- deferral policy (usage limit)
# Policy: a transient, time-bound failure (usage limit) halts the bot and leaves the message
# unprocessed for the next Start. A permanent failure is skipped and announced. See
# handlers/base.DeferMessage and docs/PHASE2.md.
import pytest

from contextbot.core import client_service as cs
from contextbot.handlers.base import DeferMessage, HandlerResult


def _route_failing_on(bad_id: int, exc: Exception):
    """Fake route that raises `exc` for one message id and succeeds for the rest."""
    calls: list[int] = []

    async def _route(incoming, settings):
        calls.append(incoming.message_id)
        if incoming.message_id == bad_id:
            raise exc
        return HandlerResult(reply="ok")

    return _route, calls


async def test_usage_limit_halts_catchup_and_leaves_message_unprocessed(
    settings, tmp_path, monkeypatch
):
    route, calls = _route_failing_on(2, DeferMessage("usage limit reached"))
    monkeypatch.setattr(cs, "route", route)
    msgs = [text_message(1, "하나"), text_message(2, "둘"), text_message(3, "셋")]
    svc, client, bot, hwm = _make(settings, msgs, tmp_path, hwm_start=0)

    await svc.start()

    assert calls == [1, 2]                      # stopped at the deferral; 3 never attempted
    assert hwm.value == 1                       # HWM left *behind* the deferred message
    assert svc._status.status == BotStatus.STOPPED
    assert "한도" in svc._status.message         # the UI says why, not just "중지됨"
    assert not client.is_connected()
    assert any("한도" in text for _, text in bot.sent)


async def test_deferred_message_is_replayed_on_next_start(settings, tmp_path, monkeypatch):
    """The whole point: after the limit resets, Start resumes from the deferred message."""
    msgs = [text_message(1, "하나"), text_message(2, "둘"), text_message(3, "셋")]
    hwm = HighWaterMark(tmp_path / "hwm.json")
    hwm.baseline(0)

    limited, _ = _route_failing_on(2, DeferMessage("usage limit reached"))
    monkeypatch.setattr(cs, "route", limited)
    svc1 = ClientService(settings, StatusModel(), client=FakeClient(msgs), bot=FakeBot(), hwm=hwm)
    await svc1.start()
    assert hwm.value == 1

    # Limit resets: everything succeeds now.
    ok, calls = _route_failing_on(-1, RuntimeError("unused"))
    monkeypatch.setattr(cs, "route", ok)
    svc2 = ClientService(settings, StatusModel(), client=FakeClient(msgs), bot=FakeBot(), hwm=hwm)
    await svc2.start()

    assert calls == [2, 3]                      # resumed at the deferred message, in order
    assert hwm.value == 3
    assert svc2._status.status == BotStatus.RUNNING


async def test_usage_limit_on_live_message_halts(settings, tmp_path, monkeypatch):
    route, _ = _route_failing_on(9, DeferMessage("usage limit reached"))
    monkeypatch.setattr(cs, "route", route)
    svc, client, bot, hwm = _make(settings, [], tmp_path, hwm_start=0)
    await svc.start()

    await svc._on_new_message(FakeEvent(text_message(9, "메모"), OWNER_ID))

    assert hwm.value == 0
    assert svc._status.status == BotStatus.STOPPED
    assert not client.is_connected()


async def test_permanent_failure_is_skipped_and_announced(settings, tmp_path, monkeypatch):
    """A poison message must not wedge the bot — but it must not vanish silently either."""
    route, calls = _route_failing_on(2, RuntimeError("corrupt file"))
    monkeypatch.setattr(cs, "route", route)
    msgs = [text_message(1, "하나"), text_message(2, "깨진것"), text_message(3, "셋")]
    svc, client, bot, hwm = _make(settings, msgs, tmp_path, hwm_start=0)

    await svc.start()

    assert calls == [1, 2, 3]                   # kept going
    assert hwm.value == 3
    assert svc._status.status == BotStatus.RUNNING
    skip_dm = [text for _, text in bot.sent if "id=2" in text]
    assert skip_dm, "the skipped message must be reported, not dropped silently"
    assert "건너뜁니다" in skip_dm[0]
    assert "corrupt file" in skip_dm[0]


async def test_permanent_failure_does_not_halt_on_restart(settings, tmp_path, monkeypatch):
    """Regression: skipping (not halting) is what prevents a poison message wedging every Start."""
    route, _ = _route_failing_on(2, RuntimeError("corrupt file"))
    monkeypatch.setattr(cs, "route", route)
    msgs = [text_message(2, "깨진것")]
    svc, client, bot, hwm = _make(settings, msgs, tmp_path, hwm_start=1)

    await svc.start()
    assert svc._status.status == BotStatus.RUNNING


# ---------------------------------------------------------------- review polling
# Increment 4: the bot DM is polled *only* while a review waits on the owner, and the store — not
# the handler's return value — is the truth about whether one is open. See docs/PHASE2.md.
import asyncio
from datetime import datetime, timezone

from contextbot.core.session_store import SessionStore
from contextbot.handlers.conversation import activate


class PollableBot(FakeBot):
    """A FakeBot that can also be long-polled, like the real telegram.Bot."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.update_calls: list[dict] = []

    async def get_updates(self, **kwargs):
        self.update_calls.append(kwargs)
        await asyncio.sleep(3600)  # nothing to report; hold like a real long poll
        return []


def _with_store(settings, messages, tmp_path, *, hwm_start=0):
    store = SessionStore(tmp_path / "reviews")
    hwm = HighWaterMark(tmp_path / "hwm.json")
    hwm.baseline(hwm_start)
    svc = ClientService(
        settings, StatusModel(), client=FakeClient(messages), bot=PollableBot(),
        hwm=hwm, store=store,
    )
    return svc, store


def _open_a_review(store, *, message_id=1, asked: bool = True):
    """A review with a draft. ``asked=False`` leaves it queued — drafted, but not yet asked about."""
    review = store.create(
        message_id=message_id, session_id="s",
        title="검토 중", source_date=datetime.now(timezone.utc),
    )
    review.write_draft("초안 본문")
    if asked:
        activate(review, store)
    return review


async def test_the_bot_dm_is_polled_whenever_the_app_is_up(settings, tmp_path):
    """It used to poll only while a review was open, which made talking to the bot at any other
    time a silent no-op — and silence reads the same whether the bot is stopped, broken, or simply
    uninterested. Answering without fail while the app is up is what reserves silence for the one
    thing Telegram gives no other signal for: nothing is running.

    Still safe for the reason it always was: the bot DM is **not** the capture channel, so the Bot
    API's 24h retention cannot cost a message that was never capture's to begin with.
    """
    svc, store = _with_store(settings, [text_message(1, "메모")], tmp_path)

    await svc.start()

    assert store.has_pending() is False   # nothing to review…
    assert svc._poller is not None and svc._poller.is_running   # …and it is listening anyway
    await svc.stop()


async def test_stopping_stops_the_polling(settings, tmp_path):
    """`is_active` is the client's connection, so `stop()` disconnecting is what ends the loop —
    no flag of its own to go stale."""
    svc, store = _with_store(settings, [], tmp_path)
    await svc.start()
    assert svc._poller.is_running

    await svc.stop()

    assert not svc._poller.is_running


async def test_a_started_review_turns_polling_on(settings, tmp_path, monkeypatch):
    async def _route(incoming, settings):
        _open_a_review(store, message_id=incoming.message_id)
        return HandlerResult(reply="초안이 준비됐습니다")

    svc, store = _with_store(settings, [text_message(1, "회의 메모")], tmp_path)
    monkeypatch.setattr(cs, "route", _route)

    await svc.start()

    assert svc._poller is not None and svc._poller.is_running
    await svc.stop()


async def test_a_review_opening_message_still_advances_the_hwm(settings, tmp_path, monkeypatch):
    """The HWM means *ingested*, not *note written*. Holding it until the owner accepted would
    block every later capture behind an unanswered review, and make a restart re-run the whole job
    on top of a draft that already exists."""
    async def _route(incoming, settings):
        _open_a_review(store, message_id=incoming.message_id)
        return HandlerResult(reply="초안")

    svc, store = _with_store(settings, [text_message(5, "회의 메모")], tmp_path)
    monkeypatch.setattr(cs, "route", _route)

    await svc.start()

    assert svc._hwm.value == 5
    assert svc._status.status == BotStatus.RUNNING  # and it did not halt
    await svc.stop()


async def test_a_review_open_at_startup_resumes_polling(settings, tmp_path):
    """Row 5 of the resume contract: a review survives a restart, so the owner's draft must not be
    stranded by one."""
    svc, store = _with_store(settings, [], tmp_path)
    _open_a_review(store)

    await svc.start()

    assert svc._poller is not None and svc._poller.is_running
    await svc.stop()


async def test_stopping_the_bot_stops_polling(settings, tmp_path):
    svc, store = _with_store(settings, [], tmp_path)
    _open_a_review(store)
    await svc.start()

    await svc.stop()

    assert not svc._poller.is_running


async def test_the_poller_reads_only_the_owners_dm(settings, tmp_path):
    svc, store = _with_store(settings, [], tmp_path)
    _open_a_review(store)

    await svc.start()
    for _ in range(100):
        if svc._bot.update_calls:
            break
        await asyncio.sleep(0.01)
    await svc.stop()

    assert svc._bot.update_calls, "polling must actually reach the Bot API"
    assert svc._bot.update_calls[0]["allowed_updates"] == ["message"]


async def test_a_review_whose_first_turn_died_with_the_app_is_discarded_at_startup(
    settings, tmp_path
):
    """It would otherwise hold the one-at-a-time guard against the very replay catch-up performs,
    while the poller waited for an answer to a question that was never asked."""
    svc, store = _with_store(settings, [], tmp_path)
    store.create(message_id=1, session_id="dead", title="검토 중",
                 source_date=datetime.now(timezone.utc))  # no draft → turn 1 never finished

    await svc.start()

    assert store.has_pending() is False
    await svc.stop()


# ------------------------------------------------- increment 5: the review queue
async def test_finishing_a_review_asks_about_the_next_one(settings, tmp_path):
    """The queue only works if something moves it along, and a review ends in a background task.

    Forgetting this call is the failure the whole design is exposed to: nothing raises, the suite
    stays green, and a finished meeting-note draft sits in the store that the owner is never asked
    about — and never can be, because nothing polls for a review nobody has been asked about.
    """
    svc, store = _with_store(settings, [], tmp_path)
    _open_a_review(store, message_id=1)
    _open_a_review(store, message_id=2, asked=False)
    await svc.start()

    await svc._on_bot_dm("취소", True)

    assert store.pending().message_id == 2
    assert any("다음 회의록 차례" in text for _, text in svc._bot.sent)
    await svc.stop()


async def test_polling_survives_the_handover_to_the_next_review(settings, tmp_path):
    """The poll loop re-checks `is_active` the moment `_on_bot_dm` returns, so promotion has
    to happen *before* it does — otherwise the loop stops with a review still queued."""
    svc, store = _with_store(settings, [], tmp_path)
    _open_a_review(store, message_id=1)
    _open_a_review(store, message_id=2, asked=False)
    await svc.start()

    await svc._on_bot_dm("취소", True)

    assert svc._poller.is_running
    assert store.has_pending()
    await svc.stop()


async def test_a_queued_review_left_by_a_restart_is_asked_about_at_startup(settings, tmp_path):
    """If the review ahead of it ended just before the app closed, nothing else will ever ask:
    promotion runs when a review ends, and that already happened."""
    svc, store = _with_store(settings, [], tmp_path)
    _open_a_review(store, message_id=2, asked=False)

    await svc.start()

    assert store.pending().message_id == 2
    assert svc._poller is not None and svc._poller.is_running
    await svc.stop()


async def test_nothing_is_promoted_while_a_review_is_still_open(settings, tmp_path):
    svc, store = _with_store(settings, [], tmp_path)
    _open_a_review(store, message_id=1)
    _open_a_review(store, message_id=2, asked=False)

    await svc.start()

    assert store.pending().message_id == 1
    assert [r.message_id for r in store.queued()] == [2]
    await svc.stop()


# --------------------------------- increment 5: the 13 minutes of silence
async def test_a_recording_is_acknowledged_before_the_slow_work_starts(settings, tmp_path, fake_stt):
    """Measured on the first real recording: 13 minutes between sending a meeting and the review
    block (320s of Whisper, then 423s of drafting), with the bot silent throughout. The owner
    reasonably concluded it was broken."""
    svc, client, bot, hwm = _make(settings, [voice_message(7)], tmp_path, hwm_start=0)

    await svc.start()

    assert bot.sent, "the owner must hear something before a multi-minute job"
    assert "녹음을 받았습니다" in bot.sent[0][1]
    # …and the real reply still follows it.
    assert len(bot.sent) == 2
    assert "저장됨" in bot.sent[1][1]


async def test_only_audio_is_acknowledged(settings, tmp_path):
    """Every other pipeline answers in seconds — an ack there is just noise."""
    svc, client, bot, hwm = _make(settings, [text_message(7, "메모")], tmp_path, hwm_start=0)

    await svc.start()

    assert len(bot.sent) == 1
    assert "녹음을 받았습니다" not in bot.sent[0][1]


# ------------------------------- the bot answers every message, without exception
async def test_a_dm_with_no_review_open_gets_a_status_reply(settings, tmp_path):
    """The silent no-op this replaces. Telegram has no offline autoresponder to borrow — a bot is a
    token plus your code — so answering *without fail* while the app is up is the closest honest
    substitute, and it is what makes silence mean "nothing is running" rather than one of three
    indistinguishable things."""
    svc, store = _with_store(settings, [], tmp_path)
    await svc.start()

    await svc._on_bot_dm("안녕?", True)

    assert any("검토 중인 초안이 없습니다" in text for _, text in svc._bot.sent)
    assert any("Saved Messages" in text for _, text in svc._bot.sent)
    await svc.stop()


async def test_a_dm_while_a_review_is_open_says_what_it_is_waiting_for(settings, tmp_path):
    """Anything that is not 확인/취소/a correction still gets an answer — here, the one that tells
    the owner what the bot is holding."""
    svc, store = _with_store(settings, [], tmp_path)
    _open_a_review(store, message_id=1)
    await svc.start()
    svc._bot.sent.clear()

    await svc._on_bot_dm("얼마나 걸려?", True)

    # It went through handle_reply (a revision), so it is the review that answered — not a status.
    assert svc._bot.sent, "every message gets an answer"
    await svc.stop()


async def test_a_stale_dm_is_answered_and_says_why_it_was_not_applied(settings, tmp_path):
    """The sharp case, and the reason the poller reports the backlog rule instead of acting on it.

    App off → owner types `확인` → app starts and opens a review during catch-up → the message
    arrives predating it. Applying it would accept a draft they never saw; dropping it silently
    would rebuild the very silence this feature removes. So: answered, and told why.
    """
    svc, store = _with_store(settings, [], tmp_path)
    _open_a_review(store, message_id=1)
    await svc.start()
    svc._bot.sent.clear()

    await svc._on_bot_dm("확인", False)

    assert len(store.all_reviews()) == 1, "it must not have accepted the draft"
    assert not list(settings.inbox_dir.iterdir()), "…and must not have written the note"
    text = svc._bot.sent[-1][1]
    assert "반영하지 않았습니다" in text
    await svc.stop()


async def test_a_failing_turn_still_answers(settings, tmp_path, monkeypatch):
    """No path out of here may say nothing — silence is the one reply the owner cannot read."""
    svc, store = _with_store(settings, [], tmp_path)
    _open_a_review(store, message_id=1)
    await svc.start()
    svc._bot.sent.clear()

    async def boom(*a, **k):
        raise RuntimeError("engine on fire")

    monkeypatch.setattr(cs, "handle_reply", boom)
    await svc._on_bot_dm("고쳐주세요", True)

    assert "engine on fire" in svc._bot.sent[-1][1]
    await svc.stop()
