from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from contextbot.core.router import build_incoming_message, classify_document, route
from contextbot.handlers.base import MessageKind

from .conftest import (
    document_message,
    photo_message,
    text_message,
    voice_message,
)

SEOUL = ZoneInfo("Asia/Seoul")
# 14:30 in Seoul, and the shape of the original report: the note said 05:30.
UTC_0530 = datetime(2026, 7, 17, 5, 30, tzinfo=timezone.utc)
# 08:00 KST on the 18th — the day boundary the UTC date fell on the wrong side of.
UTC_2300_PREV_DAY = datetime(2026, 7, 17, 23, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------- classify_document
@pytest.mark.parametrize(
    "name,mime,expected",
    [
        ("note.md", None, MessageKind.MARKDOWN),
        ("note.markdown", None, MessageKind.MARKDOWN),
        ("rec.m4a", None, MessageKind.AUDIO),
        ("rec.mp3", None, MessageKind.AUDIO),
        ("shot.png", None, MessageKind.IMAGE),
        ("doc.pdf", None, MessageKind.DOCUMENT),
        ("deck.pptx", None, MessageKind.DOCUMENT),
        ("sheet.xlsx", None, MessageKind.DOCUMENT),
        (None, "audio/mpeg", MessageKind.AUDIO),
        (None, "image/png", MessageKind.IMAGE),
        (None, "text/markdown", MessageKind.MARKDOWN),
        (None, "application/octet-stream", MessageKind.DOCUMENT),
        (None, None, MessageKind.UNKNOWN),
    ],
)
def test_classify_document(name, mime, expected):
    assert classify_document(name, mime) == expected


def test_extension_wins_over_mime():
    assert classify_document("x.md", "application/octet-stream") == MessageKind.MARKDOWN


# --------------------------------------------------------- build_incoming_message
def test_build_text_message():
    msg = build_incoming_message(text_message(10, "hello"))
    assert msg.kind == MessageKind.TEXT
    assert msg.text == "hello"
    assert msg.message_id == 10
    assert msg.raw is not None


def test_build_voice_message():
    msg = build_incoming_message(voice_message(11))
    assert msg.kind == MessageKind.AUDIO


def test_build_photo_message():
    msg = build_incoming_message(photo_message(12))
    assert msg.kind == MessageKind.IMAGE


def test_build_document_pdf():
    msg = build_incoming_message(document_message(13, "report.pdf", "application/pdf"))
    assert msg.kind == MessageKind.DOCUMENT
    assert msg.file_name == "report.pdf"


def test_build_markdown_document():
    msg = build_incoming_message(document_message(14, "idea.md", "text/markdown"))
    assert msg.kind == MessageKind.MARKDOWN


def test_build_none():
    assert build_incoming_message(None) is None


# ---------------------------------------------------------------------- route
async def test_route_text_writes_note(settings):
    msg = build_incoming_message(text_message(20, "라우팅 테스트"))
    result = await route(msg, settings)
    assert result.saved_path is not None
    assert result.saved_path.exists()


async def test_route_audio_reaches_the_meeting_pipeline(settings, fake_stt):
    """Audio is no longer a stub (increment 5). With the engine off there is no meeting note to
    make, but STT is local — so the transcript is saved rather than the recording lost."""
    msg = build_incoming_message(voice_message(21))

    result = await route(msg, settings)

    assert result.saved_path is not None
    assert len(fake_stt) == 1
    assert "전사 원문" in result.reply


async def test_route_markdown_saves_the_file(settings):
    msg = build_incoming_message(document_message(22, "x.md", "text/markdown", content=b"# routed"))
    result = await route(msg, settings)

    assert result.saved_path is not None
    assert result.saved_path.read_text(encoding="utf-8") == "# routed"


async def test_every_route_is_decided_by_the_message_kind_alone(settings):
    """Increment 4 had one exception — a `#검토` prefix diverting a memo into the review loop —
    and increment 5 deleted it with the rest of that scaffolding.

    A memo that *looks* like the old trigger is now just a memo, which is the whole point: Saved
    Messages means "throw it in and it becomes a note", and a magic prefix made that conditional.
    That conditionality is what produced the `#검토된 사항` mangling bug in the first place.
    """
    for text in ("#검토 인프라 예산 회의 메모", "#검토된 사항 정리하기", "그냥 메모"):
        msg = build_incoming_message(text_message(24, text))

        result = await route(msg, settings)

        assert result.saved_path is not None
        assert result.saved_path.read_text(encoding="utf-8").strip().endswith(text)


async def test_route_unsupported_document_asks_for_a_pdf(settings):
    msg = build_incoming_message(document_message(23, "deck.pptx", content=b"data"))
    result = await route(msg, settings)

    assert result.saved_path is None
    assert "PDF로 내보내서" in result.reply


# ------------------------------------------------------- the clock a note is written on
async def test_route_puts_the_date_on_the_owners_clock(settings):
    """Telethon hands us UTC, and every render site downstream formats whatever it is given — so
    the conversion happens once, here, at the gate they all pass through."""
    msg = build_incoming_message(text_message(25, "시각 확인", date=UTC_0530))

    await route(msg, settings)

    # The handler saw 14:30 KST, not the 05:30 UTC that Telegram reported.
    assert msg.date.astimezone(SEOUL).strftime("%H:%M") == "14:30"


async def test_a_note_captured_before_9am_is_filed_under_today(settings):
    """The half of the bug nobody would have noticed for months: 08:00 KST is *yesterday* in UTC,
    so `YYMMDD-` filed a morning memo under the previous day."""
    msg = build_incoming_message(text_message(26, "아침 메모", date=UTC_2300_PREV_DAY))

    result = await route(msg, settings)

    assert result.saved_path.name.startswith("260718-"), result.saved_path.name


async def test_route_does_not_mutate_the_callers_message(settings):
    """`dataclasses.replace`, not assignment: the client still holds this object, and a route that
    edited it in place would be reaching backwards out of its own call."""
    msg = build_incoming_message(text_message(27, "원본 보존", date=UTC_0530))

    await route(msg, settings)

    assert msg.date == UTC_0530
    assert msg.date.tzinfo is timezone.utc


async def test_note_timezone_is_honoured(settings):
    """NOTE_TIMEZONE is not decoration: the owner travels, and a hardcoded +09:00 would be the
    same class of bug as the UTC it replaced."""
    msg = build_incoming_message(text_message(28, "뉴욕에서", date=UTC_0530))

    result = await route(msg, replace(settings, note_timezone=ZoneInfo("America/New_York")))

    # 05:30 UTC is 01:30 in New York — the same instant, a different day's clock than Seoul's.
    assert result.saved_path.name.startswith("260717-"), result.saved_path.name
    body = result.saved_path.read_text(encoding="utf-8")
    assert "date: '2026-07-17T01:30:00-04:00'" in body
