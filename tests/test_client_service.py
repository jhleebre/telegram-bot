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


async def test_audio_is_stub_no_file(settings, tmp_path):
    svc, client, bot, hwm = _make(settings, [voice_message(7)], tmp_path, hwm_start=0)

    await svc.start()

    assert not list(settings.inbox_dir.iterdir())
    assert bot.sent and "Phase 2" in bot.sent[0][1]
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
