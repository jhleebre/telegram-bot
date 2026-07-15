from datetime import datetime, timezone

import yaml

from contextbot.handlers.base import IncomingMessage, MessageKind
from contextbot.handlers.text_handler import handle_text


def _msg(text: str) -> IncomingMessage:
    return IncomingMessage(
        user_id=42,
        chat_id=999,
        message_id=7,
        date=datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc),
        kind=MessageKind.TEXT,
        text=text,
    )


async def test_handle_text_writes_note(settings):
    result = await handle_text(_msg("첫 줄 제목\n본문 내용"), settings)
    assert result.saved_path is not None
    assert result.saved_path.exists()
    assert "저장됨" in result.reply

    content = result.saved_path.read_text(encoding="utf-8")
    fm = yaml.safe_load(content.split("---\n")[1])
    assert fm["title"] == "첫 줄 제목"
    assert fm["telegram_message_id"] == 7
    assert "본문 내용" in content


async def test_handle_empty_text(settings):
    result = await handle_text(_msg("   "), settings)
    assert result.saved_path is None
    assert not list(settings.inbox_dir.iterdir())


async def test_long_title_truncated(settings):
    long_line = "가" * 200
    result = await handle_text(_msg(long_line), settings)
    content = result.saved_path.read_text(encoding="utf-8")
    fm = yaml.safe_load(content.split("---\n")[1])
    assert fm["title"].endswith("…")
    assert len(fm["title"]) <= 82
