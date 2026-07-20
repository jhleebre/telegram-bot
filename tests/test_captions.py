"""Caption tests: what the owner types *alongside* a file reaches the note, as intent.

Telegram allows a caption on every media message — photo, voice, audio, animation, and any
document — and only on those: a plain text message has no caption because the text *is* the
message. Telethon does not separate the two (a media message's ``raw_text`` **is** its caption), so
the split is the router's job, and these tests pin it at that seam and at every one downstream.

The behaviour under test is deliberately not "the caption is copied into the note". It is fed to
the model as the owner's instruction, folded into the note's own wording, and kept raw only in the
frontmatter — so what is asserted here is that it *reaches the prompt* and *survives in
frontmatter*, never that it appears verbatim in the body.
"""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from contextbot.core.router import build_incoming_message
from contextbot.core.session_store import PendingReview, SessionStore
from contextbot.engine import prompts
from contextbot.engine.claude_cli import ClaudeResult
from contextbot.handlers.base import IncomingMessage, MessageKind
from contextbot.handlers.document_handler import handle_document
from contextbot.handlers.image_handler import handle_image

from .conftest import document_message, photo_message, text_message, voice_message

DATE = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)
CAPTION = "어제 팀 회의에서 받은 자료야. 핵심만 짧게 정리해줘."


# --------------------------------------------------------------- the router seam
@pytest.mark.parametrize(
    "message, kind",
    [
        (photo_message(1, raw_text=CAPTION), MessageKind.IMAGE),
        (voice_message(2, raw_text=CAPTION), MessageKind.AUDIO),
        (document_message(3, "report.pdf", raw_text=CAPTION), MessageKind.DOCUMENT),
        (document_message(4, "notes.md", raw_text=CAPTION), MessageKind.MARKDOWN),
        (document_message(5, "meeting.m4a", raw_text=CAPTION), MessageKind.AUDIO),
        (document_message(6, "shot.png", raw_text=CAPTION), MessageKind.IMAGE),
    ],
)
def test_every_media_kind_carries_its_caption(message, kind):
    """Not just images. Telegram puts a caption on every attachment, so every route must see one."""
    incoming = build_incoming_message(message)
    assert incoming.kind is kind
    assert incoming.caption == CAPTION


def test_a_text_message_has_no_caption():
    """The text *is* the note here — there is nothing standing outside it to take direction from.

    Reading it as a caption would be the damaging confusion: a memo saying "이거 요약해줘" would
    become an instruction to the enricher instead of the note's own content, and the note the owner
    typed would be quietly replaced by a note *about* what they typed.
    """
    incoming = build_incoming_message(text_message(1, "회의 준비하기"))
    assert incoming.kind is MessageKind.TEXT
    assert incoming.caption == ""
    assert incoming.text == "회의 준비하기"


def test_an_uncaptioned_attachment_carries_an_empty_caption():
    incoming = build_incoming_message(photo_message(1))
    assert incoming.caption == ""


# ------------------------------------------------------------- the shared block
def test_no_caption_renders_nothing():
    """The empty string is the whole no-caption path: templates read as they did before captions."""
    assert prompts.caption_section("") == ""
    assert prompts.caption_section(None) == ""
    assert prompts.caption_section("   \n  ") == ""


def test_the_caption_block_frames_the_text_as_the_owner_speaking():
    out = prompts.caption_section(CAPTION)
    assert CAPTION in out
    assert "<caption>" in out and "</caption>" in out
    # The distinction the whole feature rests on: the file is data, the caption is an instruction.
    assert "it therefore *is* an\ninstruction" in out


def test_the_caption_block_forbids_pasting_it_into_the_note():
    """Active processing is the requirement — a caption echoed verbatim is the failure mode."""
    out = " ".join(prompts.caption_section(CAPTION).split())
    assert "Do not paste it into the note verbatim" in out


def test_the_caption_block_cannot_loosen_the_anti_fabrication_rules():
    """A caption is trusted about *intent*, never about what is in a file nobody could read."""
    out = " ".join(prompts.caption_section(CAPTION).split())
    assert "Do not let it loosen the rules below" in out
    assert "the one kind you do not follow" in out


def test_braces_in_a_caption_are_inert():
    """The caption is a *value* substituted into a template, never itself formatted as one.

    Without this, a caption mentioning `{path}` would either explode with KeyError or, far worse,
    interpolate another placeholder's value into the owner's words.
    """
    out = prompts.caption_section("{path} 랑 {sentinel} 확인해줘")
    assert "{path} 랑 {sentinel} 확인해줘" in out


@pytest.mark.parametrize(
    "name, values",
    [
        ("image_describe", {"path": "/x.png", "sentinel": "S"}),
        ("pdf_to_markdown", {"path": "/x.pdf", "sentinel": "S"}),
        ("text_enrich", {"text": "본문"}),
        (
            "meeting_note",
            {
                "transcript_path": "/x.txt",
                "glossary": "용어집",
                "meeting_date": "2026-07-15 14:30",
                "sentinel": "S",
                "questions_heading": "## 확인 요청",
            },
        ),
    ],
)
def test_every_media_template_has_a_slot_for_the_caption(name, values):
    with_caption = prompts.render(name, caption=prompts.caption_section(CAPTION), **values)
    without = prompts.render(name, caption=prompts.caption_section(""), **values)
    assert CAPTION in with_caption
    assert CAPTION not in without


def test_the_pdf_template_will_not_trade_the_conversion_for_a_summary():
    """The one place a caption and the route's purpose genuinely collide.

    "핵심만 요약해줘" on a PDF must *add* a summary, never replace the transcription: the original
    goes to ~/Downloads and the note is the only searchable copy, so a note that summarised it away
    would have discarded the document with nothing left in the vault to recover it from.
    """
    out = " ".join(prompts.render("pdf_to_markdown", path="/x.pdf", sentinel="S", caption="").split())
    assert "This survives the owner's note above" in out
    assert "Add to the conversion; never substitute for it." in out


def test_the_meeting_template_lets_a_caption_settle_a_question():
    """The review loop asks the owner about what it had to guess. A caption already answered."""
    out = " ".join(
        prompts.render(
            "meeting_note",
            transcript_path="/x.txt",
            glossary="용어집",
            meeting_date="2026-07-15 14:30",
            sentinel="S",
            questions_heading="## 확인 요청",
            caption="",
        ).split()
    )
    assert "The owner's note above settles things the same way" in out


# ----------------------------------------------------------------- the handlers
class FakeUpload:
    def __init__(self, content: bytes):
        self.content = content

    async def download_media(self, file):
        Path(file).write_bytes(self.content)
        return file


class FakeEngine:
    def __init__(self, text: str):
        self._text = text
        self.calls: list[str] = []

    async def run(self, prompt: str, **kwargs):
        self.calls.append(prompt)
        return ClaudeResult(text=self._text, session_id="s-1")


def _msg(kind: MessageKind, file_name: str | None, content: bytes, caption: str) -> IncomingMessage:
    return IncomingMessage(
        user_id=42,
        chat_id=999,
        message_id=7,
        date=DATE,
        kind=kind,
        caption=caption,
        file_name=file_name,
        raw=FakeUpload(content),
    )


def _fm(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---\n")[1])


def _body(path: Path) -> str:
    return path.read_text(encoding="utf-8").split("---\n", 2)[2]


@pytest.fixture
def fake_sips(monkeypatch):
    """Never shell out to the real `sips`. A .png needs no transcode, so this is a pass-through."""

    async def _convert(src, dest_dir):
        return src

    monkeypatch.setattr("contextbot.handlers.image_handler.normalize", _convert)


IMAGE_REPLY = "제목: 3분기 예산 검토 화면\n\n## 설명\n\n예산 검토 화면이다.\n\n## 텍스트\n\n(텍스트 없음)\n"
PDF_REPLY = "분류: 보고\n\n# 3분기 보고서\n\n본문이 여기에 들어간다. 충분히 길다.\n"


@pytest.mark.asyncio
async def test_an_images_caption_reaches_the_model_and_survives_in_frontmatter(
    settings, fake_sips
):
    engine = FakeEngine(IMAGE_REPLY)
    result = await handle_image(
        _msg(MessageKind.IMAGE, "shot.png", b"\x89PNG\r\n\x1a\n", CAPTION),
        replace(settings, claude_enabled=True),
        engine=engine,
    )
    assert CAPTION in engine.calls[0]

    fm = _fm(result.saved_path)
    # Kept raw, because acting on it is lossy — the description absorbed it, and this is the only
    # place the owner's own words survive to explain why the note reads the way it does.
    assert fm["caption"] == CAPTION
    # And *not* pasted into the body: the model was told to act on it, not file it.
    assert CAPTION not in _body(result.saved_path)


@pytest.mark.asyncio
async def test_an_uncaptioned_image_gets_no_caption_key(settings, fake_sips):
    result = await handle_image(
        _msg(MessageKind.IMAGE, "shot.png", b"\x89PNG\r\n\x1a\n", ""),
        replace(settings, claude_enabled=True),
        engine=FakeEngine(IMAGE_REPLY),
    )
    assert "caption" not in _fm(result.saved_path)


@pytest.mark.asyncio
async def test_a_caption_titles_the_stub_note_when_no_model_ran(settings, fake_sips):
    """The degraded path, where the caption is the only thing that says what the image was.

    Nothing looked at the picture here, so the alternative title is the filename — `IMG_4821` for a
    phone photo, or the bare word 이미지 for a Telegram photo that has no name at all.
    """
    result = await handle_image(
        _msg(MessageKind.IMAGE, "IMG_4821.png", b"\x89PNG\r\n\x1a\n", "영수증 정리해줘"),
        settings,  # claude_enabled=False → stub note, no model
        engine=None,
    )
    assert _fm(result.saved_path)["title"] == "영수증 정리해줘"


@pytest.mark.asyncio
async def test_a_pdfs_caption_reaches_the_conversion_and_survives_in_frontmatter(settings):
    engine = FakeEngine(PDF_REPLY)
    result = await handle_document(
        _msg(MessageKind.DOCUMENT, "report.pdf", b"%PDF-1.4", CAPTION),
        replace(settings, claude_enabled=True),
        engine=engine,
    )
    assert CAPTION in engine.calls[0]
    assert _fm(result.saved_path)["caption"] == CAPTION


@pytest.mark.asyncio
async def test_a_text_files_caption_reaches_the_enrichment_pass(settings):
    engine = FakeEngine('{"title": "3분기 자료", "tags": ["예산"], "summary": "요약", "category": "보고"}')
    result = await handle_document(
        _msg(MessageKind.DOCUMENT, "memo.txt", "회의 메모입니다.".encode("utf-8"), CAPTION),
        replace(settings, claude_enabled=True),
        engine=engine,
    )
    assert CAPTION in engine.calls[0]
    assert _fm(result.saved_path)["caption"] == CAPTION
    # The body is still the file, untouched — the caption directed the metadata, not the content.
    assert "회의 메모입니다." in _body(result.saved_path)


# ------------------------------------------------- surviving the review loop
def test_a_reviews_caption_round_trips_through_the_store(tmp_path):
    """The audio route's note is written minutes-to-days later, possibly in another process run."""
    store = SessionStore(tmp_path / "reviews")
    store.create(
        message_id=11,
        session_id="s-1",
        title="회의록 작성 중",
        source_date=DATE,
        caption=CAPTION,
    )
    reloaded = SessionStore(tmp_path / "reviews").get(11)
    assert reloaded is not None
    assert reloaded.caption == CAPTION


MEETING_DRAFT = """제목: 3분기 인프라 예산 회의
태그: 인프라, 예산

## Overview

| Field | Details |
|-------|---------|
| Date | 2026-07-15 14:30 |
| Attendees | 김철수, 이영희 |
| Purpose | 예산 검토 |

## Summary

예산을 10-20% 줄이기로 했다.

## 확인 요청

- (없음)
"""


@pytest.mark.asyncio
async def test_a_recordings_caption_reaches_the_draft_and_the_finished_note(
    settings, tmp_path, fake_stt
):
    """The longest chain in the codebase: caption → prompt → persisted review → note frontmatter.

    Worth an end-to-end test rather than three unit ones, because the note is written by a
    *different* module (the review loop) than the one that saw the caption, at a point that may be
    days and a process restart later. Every link in between has to hold or the caption silently
    stops at the draft.
    """
    from contextbot.handlers.audio_handler import handle_audio
    from contextbot.handlers.conversation import _write

    class _Raw:
        async def download_media(self, file):
            Path(file).write_bytes(b"fake audio bytes")
            return file

    message = IncomingMessage(
        user_id=1,
        chat_id=1,
        message_id=7,
        date=DATE,
        kind=MessageKind.AUDIO,
        caption="참석자는 김철수, 이영희 두 명이야. 팀웹은 T-map 오타고.",
        file_name="meeting.m4a",
        mime_type="audio/mp4",
        raw=_Raw(),
    )
    live = replace(settings, claude_enabled=True)
    store = SessionStore(tmp_path / "reviews")
    engine = FakeEngine(MEETING_DRAFT)

    await handle_audio(message, live, engine=engine, store=store)

    assert message.caption in engine.calls[0]

    review = store.get(7)
    assert review is not None and review.caption == message.caption

    path = _write(review, live)
    assert _fm(path)["caption"] == message.caption


def test_a_review_written_before_captions_existed_still_loads(tmp_path):
    """Widening the persisted schema must not strand a review that was already in flight."""
    old = {
        "message_id": 11,
        "session_id": "s-1",
        "work_dir": str(tmp_path / "work"),
        "draft_path": str(tmp_path / "draft.md"),
        "title": "회의록",
        "created_at": DATE.isoformat(),
        "source_date": DATE.isoformat(),
    }
    assert PendingReview.from_json(old).caption == ""
