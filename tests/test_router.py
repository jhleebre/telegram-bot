import pytest

from contextbot.core.router import build_incoming_message, classify_document, route
from contextbot.handlers.base import MessageKind

from .conftest import (
    document_message,
    photo_message,
    text_message,
    voice_message,
)


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
