"""Audio pipeline tests: recording → transcript → drafted meeting note → review.

No model is ever run: Whisper is the `fake_stt` fixture and the engine is a canned FakeEngine.

The load-bearing assertions here are the ones about **what is not lost**. A draft in this pipeline
is minutes of Whisper plus an LLM pass, and the transcript is the part that cannot be rebuilt from
anywhere else — so every failure path is checked for what it leaves behind, not just for what it
says.
"""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from contextbot.core.session_store import ReviewState, SessionStore
from contextbot.engine.claude_cli import ClaudeError, ClaudeResult, ClaudeUsageLimit
from contextbot.handlers.audio_handler import handle_audio
from contextbot.handlers.base import DeferMessage, IncomingMessage, MessageKind
from contextbot.stt import whisper

DATE = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)

DRAFT = """제목: 3분기 인프라 예산 회의
태그: 인프라, 예산, 3분기-계획

## Overview

| Field | Details |
|-------|---------|
| Date | 2026-07-15 14:30 |
| Attendees | 김철수 |
| Purpose | 예산 검토 |

## Summary

예산을 10-20% 줄이기로 했다.

## 확인 요청

- 「팀웹」이 「T-map」 맞나요? `[00:04]` "김철수 책임이 팀웹 안건을 맡습니다"
"""


class FakeEngine:
    """Canned turns, in order. Records how each was invoked — including the cwd and add_dirs,
    which is the only way to assert what the model could actually see."""

    def __init__(self, *turns):
        self._turns = list(turns)
        self.calls: list[dict] = []
        # The work dir's listing at *call* time. Asserted after the fact, it would show the dir the
        # handler later cleaned up — this is what the model saw when it ran.
        self.seen: list[list[str]] = []

    async def run(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        cwd = kwargs.get("cwd")
        self.seen.append(sorted(p.name for p in Path(cwd).iterdir()) if cwd else [])
        turn = self._turns.pop(0) if self._turns else ClaudeResult(text=DRAFT)
        if isinstance(turn, Exception):
            raise turn
        return turn


def audio_message(message_id: int = 7, name: str | None = "meeting.m4a", **kw) -> IncomingMessage:
    class _Raw:
        content = b"fake audio bytes"

        async def download_media(self, file):
            Path(file).write_bytes(self.content)
            return file

    return IncomingMessage(
        user_id=1,
        chat_id=1,
        message_id=message_id,
        date=DATE,
        kind=MessageKind.AUDIO,
        text="",
        file_name=name,
        mime_type="audio/mp4",
        raw=_Raw(),
        **kw,
    )


@pytest.fixture
def store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "reviews")


@pytest.fixture
def live(settings):
    return replace(settings, claude_enabled=True)


@pytest.fixture
def glossary(live) -> Path:
    live.glossary_path.write_text(
        "## 용어 목록\n\n| 전사 표현 | 정확한 표현 | 유형 | 설명 |\n|---|---|---|---|\n"
        "| 팀웹 | T-map | 제품명 | 내비 |\n",
        encoding="utf-8",
    )
    return live.glossary_path


def notes(settings) -> list[Path]:
    return sorted(settings.inbox_dir.glob("*.md"))


def note_data(settings) -> tuple[dict, str]:
    text = notes(settings)[0].read_text(encoding="utf-8")
    _, fm, body = text.split("---", 2)
    return yaml.safe_load(fm), body.strip()


def stage_dir(settings, message_id: int = 7) -> Path:
    return settings.session_path.parent / "audio" / str(message_id)


# ------------------------------------------------------------------- happy path
async def test_audio_becomes_a_draft_and_opens_a_review(live, store, fake_stt):
    engine = FakeEngine(ClaudeResult(text=DRAFT))

    result = await handle_audio(audio_message(), live, engine=engine, store=store)

    assert "초안이 준비됐습니다" in result.reply
    assert "3분기 인프라 예산 회의" in result.reply
    review = store.pending()
    assert review is not None
    assert review.state is ReviewState.AWAITING_REVIEW
    assert "예산을 10-20% 줄이기로 했다" in review.draft
    assert len(fake_stt) == 1


async def test_the_note_type_is_the_vaults_own_convention(live, store, fake_stt):
    """`meeting-note`, not `meeting`: 35 notes in the vault carry it and none carry `meeting`.

    A quiet trap — nothing fails if this is wrong, the note just lands with the wrong `type:` in its
    frontmatter and only a reader who looks would ever notice.
    """
    await handle_audio(audio_message(), live, engine=FakeEngine(), store=store)

    assert store.pending().note_type == "meeting-note"


async def test_the_tags_line_becomes_frontmatter_not_body(live, store, fake_stt):
    """Otherwise the note body opens with the literal text `태그: 인프라, 예산…`."""
    await handle_audio(audio_message(), live, engine=FakeEngine(), store=store)

    review = store.pending()
    assert review.tags == ["인프라", "예산", "3분기-계획"]
    assert "태그:" not in review.draft
    assert review.draft.startswith("## Overview")


async def test_the_title_line_is_stripped_off_the_draft(live, store, fake_stt):
    await handle_audio(audio_message(), live, engine=FakeEngine(), store=store)

    assert store.pending().title == "3분기 인프라 예산 회의"
    assert "제목:" not in store.pending().draft


async def test_a_draft_without_a_title_falls_back_to_the_filename(live, store, fake_stt):
    engine = FakeEngine(ClaudeResult(text="## Overview\n\n내용이 충분히 깁니다.\n"))

    await handle_audio(audio_message(name="주간회의.m4a"), live, engine=engine, store=store)

    assert store.pending().title == "주간회의"


async def test_a_voice_note_with_no_filename_still_gets_a_title(live, store, fake_stt):
    """A Telegram voice message carries no file name at all."""
    engine = FakeEngine(ClaudeResult(text="## Overview\n\n내용이 충분히 깁니다.\n"))

    await handle_audio(audio_message(name=None), live, engine=engine, store=store)

    assert store.pending().title == "회의록"


# ------------------------------------------------------------------- isolation
async def test_the_work_dir_holds_the_transcript_alone(live, store, fake_stt):
    """Asserted as the model saw it, at call time.

    `work_dir` is the cwd *and* the sole --add-dir, so this listing is the model's entire view of
    the filesystem. Increments 2-3: given anything else to look at, it will fabricate from a
    neighbour rather than admit it cannot read what it was asked for.
    """
    engine = FakeEngine()

    await handle_audio(audio_message(), live, engine=engine, store=store)

    assert engine.seen == [["transcript.txt"]]


async def test_the_audio_is_kept_out_of_the_models_view(live, store, fake_stt):
    """An .m4a in the work dir is a file `Read` hands back as raw bytes — the .heic trap, where the
    model describes the header and reports success. It cannot hear the recording; it has the
    transcript."""
    engine = FakeEngine()

    await handle_audio(audio_message(), live, engine=engine, store=store)

    review = store.pending()
    assert "meeting.m4a" not in engine.seen[0]
    assert (review.audio_dir / "meeting.m4a").is_file()
    assert review.audio_dir not in review.work_dir.parents


async def test_the_transcript_the_model_reads_carries_timestamps(live, store, fake_stt):
    engine = FakeEngine()

    await handle_audio(audio_message(), live, engine=engine, store=store)

    staged = (store.pending().work_dir / "transcript.txt").read_text(encoding="utf-8")
    assert "[00:00 -> 00:04]" in staged
    assert str(engine.calls[0]["cwd"]) in engine.calls[0]["prompt"]


# -------------------------------------------------------------------- glossary
async def test_the_glossary_is_a_second_add_dir_never_a_copy(live, store, fake_stt, glossary):
    """Read-only context from outside — the one exception to the isolation rule."""
    engine = FakeEngine()

    await handle_audio(audio_message(), live, engine=engine, store=store)

    add_dirs = engine.calls[0]["add_dirs"]
    assert glossary.parent in add_dirs
    assert str(glossary) in engine.calls[0]["prompt"]
    # …and it stays out of the work dir.
    assert engine.seen == [["transcript.txt"]]


async def test_no_glossary_means_no_add_dir_and_an_honest_prompt(live, store, fake_stt):
    engine = FakeEngine()

    await handle_audio(audio_message(), live, engine=engine, store=store)

    assert engine.calls[0]["add_dirs"] == [store.pending().work_dir]
    assert "There is no glossary" in engine.calls[0]["prompt"]


# ------------------------------------------------------------------- settings
async def test_the_meeting_model_and_language_come_from_settings(live, store, fake_stt):
    tuned = replace(live, claude_meeting_model="opus", whisper_language="en", whisper_model="tiny")
    engine = FakeEngine()

    await handle_audio(audio_message(), tuned, engine=engine, store=store)

    assert engine.calls[0]["model"] == "opus"
    assert fake_stt[0]["language"] == "en"
    assert fake_stt[0]["model"] == "tiny"


# ---------------------------------------------------------- the transcript cache
async def test_a_usage_limit_defers_and_leaves_no_review(live, store, fake_stt):
    """Turn 1 *is* a capture, so the usage-limit policy applies in full."""
    engine = FakeEngine(ClaudeUsageLimit("5-hour limit reached"))

    with pytest.raises(DeferMessage):
        await handle_audio(audio_message(), live, engine=engine, store=store)

    assert store.all_reviews() == []
    assert notes(live) == []


async def test_a_deferral_keeps_the_transcript_so_the_replay_skips_whisper(live, store, fake_stt):
    """The replay cost, paid once.

    A deferred message re-runs its handler from scratch, and for audio that would mean downloading
    and re-Whispering a whole meeting — minutes — to regenerate a transcript that already exists.
    The staging dir is deliberately the one thing the deferral path does **not** clean up.
    """
    with pytest.raises(DeferMessage):
        await handle_audio(
            audio_message(), live, engine=FakeEngine(ClaudeUsageLimit("limit")), store=store
        )
    assert (stage_dir(live) / "transcript.txt").is_file()

    # The replay: same message, a healthy engine this time.
    result = await handle_audio(audio_message(), live, engine=FakeEngine(), store=store)

    assert "초안이 준비됐습니다" in result.reply
    assert len(fake_stt) == 1, "Whisper must not run twice for one message"


async def test_the_staging_dir_is_cleaned_up_once_the_review_owns_the_work(live, store, fake_stt):
    await handle_audio(audio_message(), live, engine=FakeEngine(), store=store)

    assert not stage_dir(live).exists()


# ------------------------------------------------------------------ degradation
async def test_a_failed_draft_still_saves_the_transcript(live, store, fake_stt):
    """No fallback exists for a *meeting note* — but STT is local, so the transcript is already
    made and is the part nothing else can rebuild. Saving it beats losing the recording."""
    engine = FakeEngine(ClaudeError("engine unhappy"))

    result = await handle_audio(audio_message(), live, engine=engine, store=store)

    assert result.saved_path is not None
    frontmatter, body = note_data(live)
    assert frontmatter["type"] == "transcript"
    assert frontmatter["reviewed"] is False
    # The timestamped rendering, not the raw run-on text: the recording is still in Saved Messages,
    # so the timestamps are what let the owner scrub back to anything that reads oddly.
    assert "[00:00 -> 00:04] 안녕하세요. 오늘 회의를 시작하겠습니다." in body
    assert "[00:04 -> 00:09] 김철수 책임이 팀웹 안건을 맡습니다." in body
    assert "engine unhappy" in body
    assert store.all_reviews() == []
    assert not stage_dir(live).exists()


async def test_the_sentinel_saves_the_transcript_too(live, store, fake_stt):
    engine = FakeEngine(ClaudeResult(text="DRAFT_FAILED"))

    result = await handle_audio(audio_message(), live, engine=engine, store=store)

    assert note_data(live)[0]["type"] == "transcript"
    assert "전사 원문" in result.reply


async def test_a_short_stub_counts_as_a_failure(live, store, fake_stt):
    """The `< 20` chars guard every route needs: a truncation that slips past the sentinel."""
    engine = FakeEngine(ClaudeResult(text="제목: 회의"))

    await handle_audio(audio_message(), live, engine=engine, store=store)

    assert note_data(live)[0]["type"] == "transcript"


async def test_engine_off_saves_the_transcript_without_calling_the_engine(settings, store, fake_stt):
    engine = FakeEngine()

    result = await handle_audio(audio_message(), settings, engine=engine, store=store)

    assert engine.calls == []
    assert note_data(settings)[0]["type"] == "transcript"
    assert "CLAUDE_ENABLED=false" in result.reply


async def test_a_transcription_failure_writes_no_note_and_does_not_halt(live, store, monkeypatch):
    """No transcript means there is nothing to save at all. Permanent as far as this message goes
    (a missing model fails identically on a replay), so it reports and lets the HWM advance —
    halting would wedge the bot on every Start."""

    def boom(*a, **kw):
        raise whisper.TranscriptionError("Whisper 모델이 아직 없습니다")

    monkeypatch.setattr(whisper, "transcribe", boom)

    result = await handle_audio(audio_message(), live, engine=FakeEngine(), store=store)

    assert notes(live) == []
    assert result.saved_path is None
    assert "전사에 실패했습니다" in result.reply
    assert not stage_dir(live).exists()


async def test_silence_is_a_transcription_failure(live, store, monkeypatch):
    """A recording with no speech would otherwise draft a meeting note out of nothing."""
    monkeypatch.setattr(
        whisper, "transcribe", lambda *a, **kw: whisper.Transcript(text="  ", segments=[])
    )

    result = await handle_audio(audio_message(), live, engine=FakeEngine(), store=store)

    assert notes(live) == []
    assert "말소리를 찾지 못했습니다" in result.reply


# ----------------------------------------------------------------------- queue
async def test_a_second_recording_is_queued_rather_than_bounced(live, store, fake_stt):
    """The whole point of the queue.

    A memo bouncing off "one at a time" cost a re-send. An audio file bouncing off it would cost a
    re-upload **and** a whole Whisper run — so the work is done and kept, and only the asking waits.
    """
    await handle_audio(audio_message(7), live, engine=FakeEngine(), store=store)

    result = await handle_audio(audio_message(8), live, engine=FakeEngine(), store=store)

    assert "대기" in result.reply
    assert "먼저" not in result.reply  # not the increment-4 bounce
    assert len(fake_stt) == 2, "the second recording is transcribed now, not later"
    assert store.pending().message_id == 7
    assert [r.message_id for r in store.queued()] == [8]


async def test_a_queued_review_has_its_draft_already(live, store, fake_stt):
    """Promotion must be a timestamp and a DM — never an engine call that could fail."""
    await handle_audio(audio_message(7), live, engine=FakeEngine(), store=store)

    await handle_audio(audio_message(8), live, engine=FakeEngine(), store=store)

    queued = store.queued()[0]
    assert "예산을 10-20% 줄이기로 했다" in queued.draft
    assert queued.title == "3분기 인프라 예산 회의"


async def test_a_queued_review_is_not_answerable(live, store, fake_stt):
    """`pending()` must not see it: the owner has not been asked, so nothing they type answers it."""
    await handle_audio(audio_message(7), live, engine=FakeEngine(), store=store)
    await handle_audio(audio_message(8), live, engine=FakeEngine(), store=store)

    assert store.pending().message_id == 7
    assert store.queued()[0].state is ReviewState.QUEUED


async def test_queuing_a_review_does_not_erase_the_active_one(live, store, fake_stt):
    """`update()` used to write `reviews = [review]`, which was fine with one review and is a
    silent eraser with two."""
    await handle_audio(audio_message(7), live, engine=FakeEngine(), store=store)
    await handle_audio(audio_message(8), live, engine=FakeEngine(), store=store)

    assert len(store.all_reviews()) == 2


# ------------------------------- the producer contract (migrated from #검토's tests)
# These claims were verified against `handle_review_request` in increment 4. That producer is gone;
# the claims are not — they are properties of *any* producer, and audio is the only one now.
async def test_the_session_id_is_pinned_before_the_call(live, store, fake_stt):
    """Row 6 of the resume contract: the store records a resumable handle *before* the call that
    creates the session, so a crash mid-call leaves something knowable rather than an orphan."""
    engine = FakeEngine()

    await handle_audio(audio_message(), live, engine=engine, store=store)

    pinned = engine.calls[0]["session_id"]
    assert pinned and store.pending().session_id == pinned
    assert engine.calls[0].get("resume") is None


async def test_the_job_runs_in_the_reviews_stable_dir_not_a_temp_one(live, store, fake_stt):
    """The measured resume contract's central fact. A session is keyed by its cwd *path*, so the
    pattern increments 2-3 use (a TemporaryDirectory deleted when the handler returns) would kill
    the session before the owner had even read the question."""
    engine = FakeEngine()

    await handle_audio(audio_message(), live, engine=engine, store=store)

    cwd = engine.calls[0]["cwd"]
    assert cwd == store.pending().work_dir
    # And it is still there after the handler returned — the point of the exercise.
    assert Path(cwd).is_dir()
    assert (Path(cwd) / "transcript.txt").is_file()


async def test_an_unexpected_error_leaves_no_review_behind(live, store, fake_stt):
    """A half-open review would hold a place in the queue for a draft that does not exist. We are
    still alive here, so we can unwind — a *crash* cannot, which is discard_incomplete's job."""
    engine = FakeEngine(RuntimeError("a broken template"))

    with pytest.raises(RuntimeError):
        await handle_audio(audio_message(), live, engine=engine, store=store)

    assert store.all_reviews() == []
    assert not stage_dir(live).exists()


async def test_a_draft_is_not_a_note_until_the_owner_accepts_it(live, store, fake_stt):
    result = await handle_audio(audio_message(), live, engine=FakeEngine(), store=store)

    assert result.saved_path is None
    assert notes(live) == []


async def test_a_transcript_containing_the_sentinel_still_drafts(live, store, monkeypatch):
    """First-line matching, not substring: a meeting where someone says "DRAFT_FAILED" out loud
    (or reads an error code aloud) must still produce its note."""
    monkeypatch.setattr(
        whisper,
        "transcribe",
        lambda *a, **kw: whisper.Transcript(
            text="DRAFT_FAILED 라는 에러 코드에 대해 논의했습니다.",
            segments=[whisper.Segment(0.0, 3.0, "DRAFT_FAILED 라는 에러 코드에 대해 논의했습니다.")],
        ),
    )

    await handle_audio(audio_message(), live, engine=FakeEngine(), store=store)

    assert store.pending() is not None
    assert notes(live) == []


async def test_a_replayed_recording_gets_a_review_after_the_crashed_one_is_discarded(
    live, store, fake_stt
):
    """Dropping a dead review is what lets catch-up's replay work.

    The HWM never advanced (the handler died before returning), so the recording replays — and it
    must not bounce off the wreckage of its own previous attempt.
    """
    store.create(message_id=7, session_id="dead", title="회의록 작성 중", source_date=DATE)
    from contextbot.handlers.conversation import discard_incomplete

    discard_incomplete(store)

    result = await handle_audio(audio_message(7), live, engine=FakeEngine(), store=store)

    assert "대기" not in result.reply
    assert store.pending().draft


async def test_a_crash_mid_download_does_not_leave_a_truncated_recording_to_reuse(
    live, store, fake_stt
):
    """The cache trusts exactly one state: a finished download **and** its transcript.

    A DeferMessage leaves both. A *crash* mid-download leaves a truncated .m4a and no transcript —
    and ffmpeg will happily decode the valid prefix of one, so reusing it would transcribe half a
    meeting into a confident, complete-looking note. Nothing would raise.
    """
    stage = stage_dir(live)
    stage.mkdir(parents=True)
    (stage / "meeting.m4a").write_bytes(b"truncated half-written download")  # no transcript beside it

    await handle_audio(audio_message(), live, engine=FakeEngine(), store=store)

    # It re-downloaded rather than reusing the wreckage, and Whisper saw that same file (it is the
    # one the handler then moved into the review's tree).
    assert len(fake_stt) == 1
    assert (store.pending().audio_dir / "meeting.m4a").read_bytes() == b"fake audio bytes"


async def test_a_leftover_download_is_reused_when_its_transcript_is_there(live, store, fake_stt):
    """…and the deferral's own state still short-circuits both steps."""
    stage = stage_dir(live)
    stage.mkdir(parents=True)
    (stage / "meeting.m4a").write_bytes(b"already downloaded")
    (stage / "transcript.txt").write_text("이미 만들어진 전사본입니다.", encoding="utf-8")

    await handle_audio(audio_message(), live, engine=FakeEngine(), store=store)

    assert fake_stt == [], "neither the download nor Whisper should run again"
