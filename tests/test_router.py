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


async def test_route_audio_is_stub(settings):
    msg = build_incoming_message(voice_message(21))
    result = await route(msg, settings)
    assert result.saved_path is None
    assert "Phase 2" in result.reply
    assert not list(settings.inbox_dir.iterdir())


async def test_route_markdown_is_stub(settings):
    msg = build_incoming_message(document_message(22, "x.md", "text/markdown"))
    result = await route(msg, settings)
    assert "Phase 2" in result.reply
