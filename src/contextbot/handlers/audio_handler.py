"""Audio handler: a recording becomes a reviewed meeting note.

Increment 5, and the last pipeline (docs/PHASE2.md). It is the only one that uses everything: the
engine (increment 1), the shared download helper (increment 3), local STT, the glossary, and the
whole human-in-the-loop review loop (increment 4). This module is a **producer of reviews** —
`handlers/conversation.py` owns the lifecycle from the moment the draft exists, and nothing about
revising, accepting, cancelling, or expiring is written twice.

The shape, and why each part is where it is:

1. **Stage under `state/audio/<message_id>/`, and keep it across a deferral.** This is the
   transcript cache. A usage limit on the drafting turn raises `DeferMessage`, and the replay re-runs
   this handler from scratch — for audio that would mean downloading and re-Whispering a meeting,
   which is minutes. Both steps skip when their output is already there, so the replay costs only
   the turn that failed. Every other exit deletes the directory.
2. **Transcribe off the event loop.** Whisper is minutes of blocking compute; run in the handler's
   thread it would freeze the UI and stall Telethon's connection for the duration.
3. **Then hand the transcript to the review loop**, staged alone in `review.work_dir` — which is the
   cwd, the sole writable-adjacent `--add-dir`, and therefore the model's whole view of the disk. The
   glossary is the one exception: read-only context from outside, so it is a second `--add-dir`,
   never a copy.
4. **The audio waits in `review.audio_dir`**, outside `work_dir`. It dies when the review ends,
   because the review's tree does.

The rules from every prior increment still bind, and two of them bind harder here:

- **Every side effect after the last LLM call.** `DeferMessage` can only fire on the drafting turn,
  which is the only turn in this file — a review turn never defers (increment 4).
- **Never lose the work.** A draft here is minutes of Whisper plus an LLM pass, and a transcript is
  the irreplaceable part: STT is local, so the transcript survives a dead engine. Every non-limit
  failure past transcription therefore still writes the transcript as a note. Saving the raw
  transcript beats losing the recording.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path

from ..config import Settings
from ..core.session_store import SessionStore, build_store
from ..engine import prompts
from ..engine.claude_cli import ClaudeCLI, ClaudeError, ClaudeUsageLimit, build_engine
from ..notes.markdown_writer import write_note
from ..stt import whisper
from .base import DeferMessage, HandlerResult, IncomingMessage
from .conversation import (
    QUESTIONS_HEADING,
    activate,
    parse_draft,
    split_tags,
)
from .downloads import download_attachment
from .text_handler import clean_title

logger = logging.getLogger("contextbot.handlers.audio")

_SENTINEL = "DRAFT_FAILED"
_MIN_OUTPUT_CHARS = 20
_TRANSCRIPT_NAME = "transcript.txt"

# Drafting a whole meeting note from a long transcript is the heaviest text job here — a floor well
# above the default budget, and above the review loop's own 120s.
_MEETING_TIMEOUT_SEC = 600.0

# The vault's own convention, read off the vault rather than chosen: 35 notes carry
# `type: meeting-note`, none carry `type: meeting`.
NOTE_TYPE = "meeting-note"

_SYSTEM_PROMPT = (
    "You are a meeting-notes editor for a personal knowledge base. You work from a speech-recognition "
    "transcript, which mishears names and jargon. You organise what was actually said, you never "
    "invent content you were not given, and you ask about what you had to guess."
)


def _draft_failed(text: str) -> bool:
    """True when the drafting turn produced nothing usable. First-line sentinel, as everywhere."""
    stripped = text.strip()
    if len(stripped) < _MIN_OUTPUT_CHARS:
        return True
    return stripped.splitlines()[0].strip().strip("`*# ").startswith(_SENTINEL)


def _glossary_section(settings: Settings) -> str:
    """The prompt's glossary instructions, or an honest statement that there are none.

    Passed in rather than branched on inside the template, because the two cases are different
    instructions and not a substituted value: with no glossary there is nothing settled, so *every*
    uncertain term is worth flagging.
    """
    if not settings.glossary_path.is_file():
        return (
            "There is no glossary for this author, so nothing is pre-confirmed and no substitutions "
            "are expected of you. Flag every term you are unsure of, per step 2."
        )
    return (
        f"A glossary of this author's already-confirmed terms is at `{settings.glossary_path}`. "
        "**Read it before you write anything.**\n\n"
        "Its 용어 목록 table maps 전사 표현 (what the recogniser writes, often several spellings, "
        "comma-separated) to 정확한 표현 (what is actually meant). **Apply every row silently**: "
        "wherever a 전사 표현 appears in the transcript, write the 정확한 표현 in the note instead. "
        "Do not report these substitutions and do not ask about them — the author has already "
        "confirmed them, and re-asking spends their attention on a settled question. Flag only "
        "terms the glossary does **not** cover."
    )


def _stage_dir(settings: Settings, message_id: int) -> Path:
    """Where the download and the transcript live between the handler and its possible replay."""
    return settings.session_path.parent / "audio" / str(message_id)


async def _transcribe(audio: Path, settings: Settings, stage: Path) -> str:
    """The transcript text, from cache if a previous attempt already made it.

    Whisper is the expensive step and its output is a pure function of the audio, so caching it by
    message id is sound: Telegram media is immutable, and the file this reads was downloaded from
    the same message id it is keyed by.
    """
    cached = stage / _TRANSCRIPT_NAME
    if cached.is_file() and cached.stat().st_size > 0:
        logger.info("reusing the cached transcript for %s", audio.name)
        return cached.read_text(encoding="utf-8")

    # Off the event loop: this is minutes of blocking compute, and the UI and the Telethon
    # connection both live on the loop this would otherwise own for the duration.
    transcript = await asyncio.to_thread(
        whisper.transcribe,
        audio,
        model=settings.whisper_model,
        language=settings.whisper_language,
    )
    if transcript.is_empty:
        raise whisper.TranscriptionError("녹음에서 말소리를 찾지 못했습니다")

    rendered = whisper.render_transcript(
        transcript, source_name=audio.name, model=settings.whisper_model
    )
    cached.write_text(rendered, encoding="utf-8")
    return rendered


async def handle_audio(
    message: IncomingMessage,
    settings: Settings,
    *,
    engine: ClaudeCLI | None = None,
    store: SessionStore | None = None,
) -> HandlerResult:
    """A sent recording → transcript → drafted meeting note → the owner's review."""
    store = store or build_store(settings)
    stage = _stage_dir(settings, message.message_id)
    stage.mkdir(parents=True, exist_ok=True)

    try:
        audio = await _download(message, stage)
    except Exception:
        # A file we cannot fetch is a fault, not a deferral: _process skips it and tells the owner.
        # Drop the staging dir so a half-written download is not mistaken for a cache on the replay.
        _cleanup(stage)
        raise

    try:
        transcript = await _transcribe(audio, settings, stage)
    except whisper.TranscriptionError as exc:
        # No transcript means no note of any kind — there is nothing to save. Permanent as far as
        # this message goes (a missing model or a corrupt file will fail identically on a replay),
        # so report it and let the HWM advance rather than halting the bot on every Start.
        logger.warning("transcription failed for message %s: %s", message.message_id, exc)
        _cleanup(stage)
        return HandlerResult(
            reply=f"🎙 회의록을 만들지 못했습니다 — 전사에 실패했습니다.\n원인: {exc}\n"
            "(원본은 Saved Messages에 그대로 있습니다)"
        )

    if not settings.claude_enabled:
        # STT is local, so the transcript survived a disabled engine. It is the expensive,
        # irreplaceable part — save it rather than losing the recording's content.
        return _transcript_note(
            message, settings, transcript, stage, "Claude 엔진이 꺼져 있습니다 (CLAUDE_ENABLED=false)"
        )

    return await _draft(message, settings, transcript, stage, engine=engine, store=store)


async def _download(message: IncomingMessage, stage: Path) -> Path:
    """The audio on disk, reusing a previous attempt's download if there is one."""
    existing = [p for p in stage.iterdir() if p.is_file() and p.name != _TRANSCRIPT_NAME]
    if existing:
        logger.info("reusing the cached download %s", existing[0].name)
        return existing[0]
    return await download_attachment(message, stage)


async def _draft(
    message: IncomingMessage,
    settings: Settings,
    transcript: str,
    stage: Path,
    *,
    engine: ClaudeCLI | None,
    store: SessionStore,
) -> HandlerResult:
    """Run the drafting turn and open (or queue) the review. The producer pattern, followed."""
    # Pin the session id and record the handle *before* the call that creates the session (measured:
    # --session-id is honoured, and resuming does not fork it). Created QUEUED, so a reply arriving
    # during the minutes this turn takes cannot be routed into a review with no draft.
    review = store.create(
        message_id=message.message_id,
        session_id=str(uuid.uuid4()),
        title="회의록 작성 중",
        source_date=message.date,
        note_type=NOTE_TYPE,
    )

    # Stage the transcript alone in the work dir: it is the cwd *and* the only --add-dir, so this
    # listing is the model's entire view of the filesystem. The audio deliberately stays out of it
    # (see PendingReview.audio_dir) — it is unreadable to the model, and a file it would `Read` into
    # raw bytes is exactly how increment 3's .heic fabrication happened.
    (review.work_dir / _TRANSCRIPT_NAME).write_text(transcript, encoding="utf-8")

    engine = engine or build_engine(settings)
    add_dirs = [review.work_dir]
    if settings.glossary_path.is_file():
        # The one genuine exception to the isolation rule: read-only context from outside, so it is
        # a second --add-dir rather than a copy into the model's view.
        add_dirs.append(settings.glossary_path.parent)

    try:
        result = await engine.run(
            prompts.render(
                "meeting_note",
                transcript_path=str(review.work_dir / _TRANSCRIPT_NAME),
                glossary=_glossary_section(settings),
                meeting_date=message.date.strftime("%Y-%m-%d %H:%M"),
                sentinel=_SENTINEL,
                questions_heading=QUESTIONS_HEADING,
            ),
            system_prompt=_SYSTEM_PROMPT,
            session_id=review.session_id,
            add_dirs=add_dirs,
            cwd=review.work_dir,
            model=settings.claude_meeting_model,
            timeout_sec=max(settings.claude_timeout_sec, _MEETING_TIMEOUT_SEC),
        )
        if _draft_failed(result.text):
            raise ClaudeError("회의록 초안을 만들지 못했습니다")
    except ClaudeUsageLimit as exc:
        # Turn 1 *is* a capture, so the usage-limit policy applies in full. Unwind the review first
        # — a replay that found a live one would queue a second copy of the same meeting — but
        # **keep the staging dir**: it holds the transcript, and it is the entire reason the replay
        # will not re-run Whisper.
        store.remove(review.message_id)
        logger.warning("usage limit; deferring message %s: %s", message.message_id, exc)
        raise DeferMessage(str(exc)) from exc
    except ClaudeError as exc:
        store.remove(review.message_id)
        logger.warning("meeting draft failed for message %s: %s", message.message_id, exc)
        return _transcript_note(message, settings, transcript, stage, str(exc))
    except Exception:
        # Anything unforeseen must not leave a half-open review behind: it would hold a place in the
        # queue for a draft that does not exist. We are still alive, so we can unwind.
        store.remove(review.message_id)
        _cleanup(stage)
        raise

    title, body, questions = parse_draft(result.text)
    tags, body = split_tags(body)
    review.title = title or _fallback_title(message)
    review.tags = tags
    review.questions = questions
    review.write_draft(body)

    # The audio moves into the review's tree, where it lives exactly as long as the review does and
    # is deleted by its ending — on accept, cancel, delivery, or expiry alike. Nothing has to
    # remember to do it, which is the only version of this that cannot leak.
    _keep_audio(stage, review.audio_dir)
    _cleanup(stage)

    if store.has_pending():
        # Another review is already being asked about. The expensive work is done and kept; only
        # the asking waits, so this costs the owner nothing but their turn in the queue.
        store.update(review)
        position = len(store.queued())
        logger.info("review %s queued behind another (%d waiting)", review.message_id, position)
        return HandlerResult(
            reply=f"🎙 전사와 초안까지 끝냈습니다 — 「{review.title}」\n"
            f"진행 중인 검토가 끝나면 이어서 물어보겠습니다. (대기 {position}번째)"
        )

    return HandlerResult(reply=activate(review, store))


def _fallback_title(message: IncomingMessage) -> str:
    if message.file_name:
        title = clean_title(Path(message.file_name).stem)
        if title:
            return title
    return "회의록"


def _keep_audio(stage: Path, audio_dir: Path) -> None:
    """Move the recording into the review's tree, to be deleted when the review ends."""
    audio_dir.mkdir(parents=True, exist_ok=True)
    for path in stage.iterdir():
        if path.is_file() and path.name != _TRANSCRIPT_NAME:
            shutil.move(str(path), str(audio_dir / path.name))


def _cleanup(stage: Path) -> None:
    """Drop the staging dir. Never called on the deferral path — that is what the cache is."""
    shutil.rmtree(stage, ignore_errors=True)


def _transcript_note(
    message: IncomingMessage,
    settings: Settings,
    transcript: str,
    stage: Path,
    reason: str,
) -> HandlerResult:
    """No meeting note is possible → save the transcript. Never lose the recording's content.

    The audio pipeline's degradation, decided in the handoff and unchanged: there is no no-LLM path
    to a *meeting note*, but STT is local, so the transcript is already made and is the part that
    cannot be reconstructed from anywhere else. A transcript is a poor note and an excellent record.
    """
    path = write_note(
        inbox_dir=settings.inbox_dir,
        body=f"> ⚠️ 회의록을 만들지 못해 전사 원문만 저장했습니다 — {reason}\n\n{transcript}",
        title=_fallback_title(message),
        when=message.date,
        source="telegram",
        note_type="transcript",
        tags=[],
        extra={"telegram_message_id": message.message_id, "reviewed": False},
    )
    _cleanup(stage)
    return HandlerResult(
        reply=f"🎙 저장됨: {path.name}\n⚠️ 회의록 대신 전사 원문만 저장했습니다 — {reason}",
        saved_path=path,
    )
