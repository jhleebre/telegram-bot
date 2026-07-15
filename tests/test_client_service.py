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
