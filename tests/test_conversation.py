"""Review loop tests: draft → ask → revise → accept, and every way it can end.

No model is ever run: the engine is a fake returning canned turns. What these assert is the state
machine's promises, and the load-bearing one is negative — **a review never ends without the
owner's draft in their hands**, except when they said 취소.
"""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from contextbot.core.session_store import ReviewState, SessionStore
from contextbot.engine.claude_cli import (
    ClaudeError,
    ClaudeResult,
    ClaudeSessionLost,
    ClaudeTimeout,
    ClaudeUsageLimit,
)
from contextbot.handlers.base import DeferMessage, IncomingMessage, MessageKind
from contextbot.handlers.conversation import (
    QUESTIONS_HEADING,
    discard_incomplete,
    expire_stale,
    handle_reply,
    handle_review_request,
    is_review_request,
    parse_draft,
    review_block,
)

DATE = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)

DRAFT = f"""제목: 3분기 인프라 예산 회의

## 결정사항

- 서버 임대 비용을 10-20% 줄인다.
- 담당자는 다음 주까지 정한다.

{QUESTIONS_HEADING}

- 담당자를 누구로 할까요?
- "다음 주"는 어느 날짜인가요?
"""

REVISED = f"""제목: 3분기 인프라 예산 회의

## 결정사항

- 서버 임대 비용을 10-20% 줄인다.
- 담당자는 김철수, 기한은 2026-07-24.

{QUESTIONS_HEADING}

- (없음)
"""


class FakeEngine:
    """Canned turns, in order. Records how each was invoked."""

    def __init__(self, *turns):
        self._turns = list(turns)
        self.calls: list[dict] = []

    async def run(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        turn = self._turns.pop(0) if self._turns else ClaudeResult(text=DRAFT)
        if isinstance(turn, Exception):
            raise turn
        return turn


def memo(text: str = "#검토 인프라 예산 회의 메모", **kw) -> IncomingMessage:
    return IncomingMessage(
        user_id=1, chat_id=1, message_id=kw.pop("message_id", 7), date=DATE,
        kind=MessageKind.TEXT, text=text, **kw
    )


@pytest.fixture
def store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "reviews")


@pytest.fixture
def live(settings):
    """Settings with the engine on — the review route needs it."""
    return replace(settings, claude_enabled=True)


def notes(settings) -> list[Path]:
    return sorted(settings.inbox_dir.glob("*.md"))


def note_data(settings) -> tuple[dict, str]:
    text = notes(settings)[0].read_text(encoding="utf-8")
    _, fm, body = text.split("---", 2)
    return yaml.safe_load(fm), body.strip()


# ------------------------------------------------------------------- the trigger
@pytest.mark.parametrize(
    "text, expected",
    [
        ("#검토 회의 메모", True),
        ("  #검토 회의 메모", True),
        ("#검토\n회의 메모", True),  # a newline is a word boundary too
        ("#검토", True),  # the trigger alone → "검토할 내용이 없습니다"
        ("회의 메모", False),
        ("메모에 #검토 라고 적었다", False),  # only a *prefix* opts in
        ("", False),
        # The prefix must be a whole word. `#검토된 사항` is a memo *about* something reviewed —
        # and a bare startswith would also have handed the model `된 사항`, drafting a note from
        # mangled text.
        ("#검토된 사항 정리하기", False),
        ("#검토사항 정리", False),
        # A Markdown H1 has a space after the hash, so it was never a trigger.
        ("# 검토 회의", False),
    ],
)
def test_is_review_request(text, expected):
    assert is_review_request(text) is expected


async def test_a_memo_starting_with_a_similar_word_is_not_mangled(settings, store):
    """The failure the word-boundary check prevents: diverted *and* silently truncated."""
    from contextbot.core.router import route

    result = await route(memo("#검토된 사항 정리하기"), settings)

    assert store.has_pending() is False
    assert result.saved_path.read_text(encoding="utf-8").strip().endswith("#검토된 사항 정리하기")


async def test_a_plain_memo_never_enters_the_review_loop(live, store):
    """The route is opt-in: the shipped one-shot path must be untouched by this increment."""
    from contextbot.core.router import route

    engine = FakeEngine()
    await route(memo("그냥 메모입니다"), replace(live, claude_enabled=False))

    assert engine.calls == []
    assert store.has_pending() is False


# ----------------------------------------------------------------------- parsing
def test_parse_draft_splits_title_body_and_questions():
    title, body, questions = parse_draft(DRAFT)
    assert title == "3분기 인프라 예산 회의"
    assert "서버 임대 비용" in body
    assert QUESTIONS_HEADING not in body
    assert "담당자를 누구로 할까요?" in questions


def test_parse_draft_tolerates_a_missing_questions_heading():
    """A malformed reply must never cost a draft the model actually produced."""
    title, body, questions = parse_draft("제목: 회의\n\n본문입니다")
    assert (title, body, questions) == ("회의", "본문입니다", "")


def test_parse_draft_tolerates_a_missing_title():
    title, body, _ = parse_draft("본문만 있습니다")
    assert title is None
    assert body == "본문만 있습니다"


def test_the_questions_never_leak_into_the_note_body(live, store):
    """The questions are scaffolding for the review, not content of the note."""
    _, body, _ = parse_draft(DRAFT)
    assert "담당자를 누구로 할까요?" not in body


def test_review_block_elides_a_draft_too_long_for_telegram(store):
    """Telegram rejects >4096 chars, and Notifier swallows the rejection — so an oversized block
    is one the owner never sees while the store still waits for their answer."""
    review = store.create(message_id=7, session_id="s", title="긴 초안", source_date=DATE)
    review.write_draft("가" * 9000)

    block = review_block(review)
    assert len(block) < 4096
    assert "생략" in block


def test_the_questions_survive_a_long_draft(store):
    """The questions are the part the owner has to answer; the draft is the part they can read in
    the note. So the draft yields the room, not the questions."""
    review = store.create(message_id=7, session_id="s", title="긴 초안", source_date=DATE)
    review.write_draft("가" * 9000)
    review.questions = "- 담당자는 누구인가요?\n- 기한은 언제인가요?"

    block = review_block(review)
    assert len(block) < 4096
    assert "담당자는 누구인가요?" in block
    assert "기한은 언제인가요?" in block
    assert "확인" in block and "취소" in block  # …and so does the instruction footer


def test_the_block_is_deliverable_even_when_the_questions_alone_are_huge(store):
    """Bounding only the draft leaves the total unbounded: the questions ride along, and a model
    that ignores "two to four" can produce a lot of them. An undeliverable block is an
    unanswerable review — worse than a truncated one."""
    review = store.create(message_id=7, session_id="s", title="t", source_date=DATE)
    review.write_draft("짧은 본문")
    review.questions = "\n".join(f"- 질문 {i}: {'가' * 200}" for i in range(40))

    assert len(review_block(review)) <= 4096


def test_review_block_hides_an_empty_question_list(store):
    review = store.create(message_id=7, session_id="s", title="t", source_date=DATE)
    review.write_draft("본문")
    review.questions = "- (없음)"
    assert "(없음)" not in review_block(review)


# ------------------------------------------------------------------ starting one
async def test_a_review_request_drafts_and_asks(live, store):
    engine = FakeEngine(ClaudeResult(text=DRAFT))
    result = await handle_review_request(memo(), live, engine=engine, store=store)

    assert "담당자를 누구로 할까요?" in result.reply
    assert "확인" in result.reply and "취소" in result.reply
    # Nothing is written yet: a draft is not a note until the owner accepts it.
    assert result.saved_path is None
    assert notes(live) == []

    review = store.pending()
    assert review.title == "3분기 인프라 예산 회의"
    assert review.state is ReviewState.AWAITING_REVIEW


async def test_the_session_id_is_pinned_before_the_call(live, store):
    """Row 6 of the resume contract, and the reason it matters: the store records a resumable
    handle *before* the call that creates the session, so a crash mid-call leaves something to
    resume rather than an orphan."""
    engine = FakeEngine(ClaudeResult(text=DRAFT))
    await handle_review_request(memo(), live, engine=engine, store=store)

    pinned = engine.calls[0]["session_id"]
    assert pinned and store.pending().session_id == pinned
    assert engine.calls[0].get("resume") is None


async def test_the_job_runs_in_the_reviews_stable_dir_not_a_temp_one(live, store):
    """The whole increment turns on this. A session is keyed by its cwd path, so the pattern
    increments 2-3 use (TemporaryDirectory, deleted when the handler returns) would kill the
    session before the owner had read the question."""
    engine = FakeEngine(ClaudeResult(text=DRAFT))
    await handle_review_request(memo(), live, engine=engine, store=store)

    cwd = engine.calls[0]["cwd"]
    assert cwd == store.pending().work_dir
    assert engine.calls[0]["add_dirs"] == [cwd]
    # And it is still there after the handler returned — the point of the exercise.
    assert Path(cwd).is_dir()


async def test_the_review_clock_starts_when_the_owner_is_asked(live, store):
    """created_at gates the poller's backlog filter, so it must mean "the earliest moment this
    could have been answered" — i.e. when the question went out, not when drafting began.

    Those are a whole turn apart: ~60s for a memo, and *minutes* for audio once Whisper is in the
    path. Anything the owner types at the bot while it is drafting predates the question and cannot
    be an answer to it — but stamped before the call, it sails through the filter and is applied as
    a correction, burning an LLM turn revising the note against "얼마나 걸려?".
    """
    drafted_at: datetime | None = None

    class SlowEngine:
        calls: list = []

        async def run(self, prompt, **kwargs):
            nonlocal drafted_at
            await asyncio.sleep(0.02)  # the draft turn takes real time
            drafted_at = datetime.now(timezone.utc)
            return ClaudeResult(text=DRAFT)

    await handle_review_request(memo(), live, engine=SlowEngine(), store=store)

    assert store.pending().created_at >= drafted_at


async def test_only_one_review_at_a_time(live, store):
    """Two open reviews make a bare "확인" unattributable. Reply and advance rather than defer:
    deferring halts the bot, which stops the poller and leaves the open review unanswerable."""
    engine = FakeEngine(ClaudeResult(text=DRAFT), ClaudeResult(text=DRAFT))
    await handle_review_request(memo(message_id=7), live, engine=engine, store=store)

    result = await handle_review_request(memo(message_id=8), live, engine=engine, store=store)

    assert "이미 검토 중" in result.reply
    assert store.pending().message_id == 7  # the first review is untouched
    assert len(engine.calls) == 1


async def test_an_empty_memo_asks_for_content(live, store):
    result = await handle_review_request(memo("#검토"), live, engine=FakeEngine(), store=store)
    assert "검토할 내용이 없습니다" in result.reply
    assert store.has_pending() is False


async def test_engine_off_saves_a_plain_note_rather_than_losing_the_memo(settings, store):
    """No engine means no draft, and a review of nothing is nothing. The capture is the point."""
    result = await handle_review_request(memo(), settings, store=store)

    assert result.saved_path is not None
    assert "CLAUDE_ENABLED=false" in result.reply
    assert store.has_pending() is False
    _, body = note_data(settings)
    assert body == "인프라 예산 회의 메모"


async def test_a_failed_first_draft_still_saves_the_memo(live, store):
    engine = FakeEngine(ClaudeTimeout("too slow"))
    result = await handle_review_request(memo(), live, engine=engine, store=store)

    assert result.saved_path is not None
    assert "검토 없이 저장했습니다" in result.reply
    assert store.has_pending() is False


async def test_the_sentinel_falls_back_to_a_plain_note(live, store):
    engine = FakeEngine(ClaudeResult(text="DRAFT_FAILED"))
    result = await handle_review_request(memo(), live, engine=engine, store=store)
    assert result.saved_path is not None
    assert store.has_pending() is False


async def test_a_memo_containing_the_sentinel_still_drafts(live, store):
    """The sentinel is matched on the first line, not by substring — the same guard the PDF and
    image routes need, for the same reason."""
    body = f"제목: 에러 코드 정리\n\nDRAFT_FAILED 는 초안 실패를 뜻한다.\n\n{QUESTIONS_HEADING}\n\n- (없음)"
    engine = FakeEngine(ClaudeResult(text=body))
    await handle_review_request(memo(), live, engine=engine, store=store)
    assert store.has_pending() is True


# -------------------------------------------------------------- the usage limit
async def test_a_usage_limit_on_turn_1_defers(live, store):
    """Turn 1 *is* a capture, so the usage-limit policy applies here in full."""
    engine = FakeEngine(ClaudeUsageLimit("429"))
    with pytest.raises(DeferMessage):
        await handle_review_request(memo(), live, engine=engine, store=store)


async def test_a_deferral_leaves_no_review_behind(live, store):
    """The replay re-runs the handler from scratch. A store entry left behind would bounce the
    replay off the one-at-a-time guard, and the memo would be answered with "이미 검토 중" forever."""
    engine = FakeEngine(ClaudeUsageLimit("429"))
    with pytest.raises(DeferMessage):
        await handle_review_request(memo(), live, engine=engine, store=store)

    assert store.has_pending() is False
    assert notes(live) == []


async def test_an_unexpected_error_leaves_no_review_behind(live, store):
    """A leaked review is worse than the error that caused it: it would block every later review
    on the one-at-a-time guard, with the poller waiting on a draft that does not exist."""
    engine = FakeEngine(RuntimeError("a bug, not a ClaudeError"))

    with pytest.raises(RuntimeError):
        await handle_review_request(memo(), live, engine=engine, store=store)

    assert store.has_pending() is False


# ----------------------------------------------------- crash during the first turn
def test_discard_incomplete_drops_a_review_whose_first_turn_never_finished(store):
    """The store entry is written before the first call, so a crash mid-call leaves a review with
    no draft — no question was asked, nothing exists to deliver. It must not hold the guard."""
    store.create(message_id=7, session_id="s", title="검토 중", source_date=DATE)

    assert discard_incomplete(store) is True
    assert store.has_pending() is False


def test_discard_incomplete_keeps_a_live_review(store):
    review = store.create(message_id=7, session_id="s", title="t", source_date=DATE)
    review.write_draft("실제 초안")

    assert discard_incomplete(store) is False
    assert store.has_pending() is True


def test_discard_incomplete_keeps_a_review_whose_draft_is_legitimately_empty(store):
    """The draft *file* is the signal, not its content: a completed turn that produced an empty
    body is still a live review, and the owner has already been asked about it."""
    review = store.create(message_id=7, session_id="s", title="t", source_date=DATE)
    review.write_draft("")

    assert discard_incomplete(store) is False
    assert store.has_pending() is True


def test_discard_incomplete_with_no_review_is_a_no_op(store):
    assert discard_incomplete(store) is False


async def test_a_turn_that_died_with_the_app_does_not_wedge_the_review(live, store):
    """FINALIZING is persisted, so a crash mid-turn would leave it set forever.

    No turn can be in flight in a fresh process, so the guard against concurrent replies must not
    outlive the process that needed it — otherwise every later reply (including `확인` and `취소`)
    is answered "잠시 후 다시 보내주세요" until the 24h expiry, and the owner cannot rescue their own
    draft. That is the stranding this state machine exists to prevent, one state over.
    """
    await _open_review(live, store)
    review = store.pending()
    review.state = ReviewState.FINALIZING  # as a crash mid-_revise would leave it
    store.update(review)

    discard_incomplete(store)

    assert store.pending().state is ReviewState.AWAITING_REVIEW
    assert await handle_reply("확인", live, store=store, engine=FakeEngine()) is not None
    assert len(notes(live)) == 1


async def test_a_replayed_memo_gets_a_review_after_the_crashed_one_is_discarded(live, store):
    """The whole point: dropping the dead review is what lets catch-up's replay actually work.

    The HWM never advanced (the handler died before returning), so the memo replays — and it must
    not be answered with "이미 검토 중" by the wreckage of its own previous attempt.
    """
    store.create(message_id=7, session_id="dead", title="검토 중", source_date=DATE)
    discard_incomplete(store)

    result = await handle_review_request(
        memo(message_id=7), live, engine=FakeEngine(ClaudeResult(text=DRAFT)), store=store
    )

    assert "이미 검토 중" not in result.reply
    assert store.pending().draft


# ----------------------------------------------------------- accepting the draft
async def _open_review(live, store, engine=None) -> None:
    await handle_review_request(
        memo(), live, engine=engine or FakeEngine(ClaudeResult(text=DRAFT)), store=store
    )


@pytest.mark.parametrize("word", ["확인", "ok", "OK", "네", "저장", "확인!", "확인.", "ok!"])
async def test_accepting_writes_the_note_and_ends_the_review(live, store, word):
    await _open_review(live, store)
    engine = FakeEngine()

    reply = await handle_reply(word, live, store=store, engine=engine)

    assert "저장됨" in reply
    assert len(notes(live)) == 1
    assert store.has_pending() is False
    # Accepting needs no engine call: the draft on disk *is* what the owner just approved.
    assert engine.calls == []


async def test_an_accepted_note_is_marked_reviewed_and_dated_by_the_memo(live, store):
    await _open_review(live, store)
    await handle_reply("확인", live, store=store, engine=FakeEngine())

    frontmatter, body = note_data(live)
    assert frontmatter["reviewed"] is True
    assert frontmatter["telegram_message_id"] == 7
    assert frontmatter["title"] == "3분기 인프라 예산 회의"
    assert "2026-07-15" in str(frontmatter["date"])
    assert "미검토" not in body
    assert QUESTIONS_HEADING not in body


async def test_accepting_removes_the_work_dir(live, store):
    await _open_review(live, store)
    work_dir = store.pending().work_dir
    await handle_reply("확인", live, store=store, engine=FakeEngine())
    assert not work_dir.exists()


# ------------------------------------------------------------------ cancelling
async def test_cancelling_discards_the_draft(live, store):
    """The one case that discards: the intent is unambiguous, and Saved Messages still holds the
    memo, so the loss is recoverable by re-sending."""
    await _open_review(live, store)

    reply = await handle_reply("취소", live, store=store, engine=FakeEngine())

    assert "취소" in reply
    assert notes(live) == []
    assert store.has_pending() is False


# -------------------------------------------------------------- revising a draft
async def test_a_correction_resumes_the_same_session(live, store):
    engine = FakeEngine(ClaudeResult(text=DRAFT), ClaudeResult(text=REVISED))
    await _open_review(live, store, engine)
    session_id = store.pending().session_id

    reply = await handle_reply("담당자는 김철수, 기한은 7월 24일", live, store=store, engine=engine)

    assert engine.calls[1]["resume"] == session_id
    assert engine.calls[1]["cwd"] == store.pending().work_dir
    assert "김철수" in reply
    assert "김철수" in store.pending().draft


async def test_the_correction_text_reaches_the_prompt(live, store):
    engine = FakeEngine(ClaudeResult(text=DRAFT), ClaudeResult(text=REVISED))
    await _open_review(live, store, engine)

    await handle_reply("담당자는 김철수", live, store=store, engine=engine)
    assert "담당자는 김철수" in engine.calls[1]["prompt"]


async def test_a_revision_loops_back_to_awaiting_not_to_a_note(live, store):
    """The owner reviews a revision before it becomes a note — a correction is not an acceptance."""
    engine = FakeEngine(ClaudeResult(text=DRAFT), ClaudeResult(text=REVISED))
    await _open_review(live, store, engine)

    await handle_reply("담당자는 김철수", live, store=store, engine=engine)

    assert notes(live) == []
    assert store.pending().state is ReviewState.AWAITING_REVIEW


async def test_the_review_can_be_accepted_after_a_revision(live, store):
    engine = FakeEngine(ClaudeResult(text=DRAFT), ClaudeResult(text=REVISED))
    await _open_review(live, store, engine)
    await handle_reply("담당자는 김철수", live, store=store, engine=engine)

    await handle_reply("확인", live, store=store, engine=engine)

    _, body = note_data(live)
    assert "김철수" in body
    assert store.has_pending() is False


async def test_a_turn_in_flight_rejects_a_second_reply(live, store):
    """Two CLI processes resuming one transcript is not something to find out about in production."""
    await _open_review(live, store)
    review = store.pending()
    review.state = ReviewState.FINALIZING
    store.update(review)

    engine = FakeEngine()
    reply = await handle_reply("또 다른 수정", live, store=store, engine=engine)

    assert "잠시 후" in reply
    assert engine.calls == []


async def test_a_reply_with_no_review_open_is_ignored(live, store):
    assert await handle_reply("확인", live, store=store, engine=FakeEngine()) is None


async def test_an_empty_reply_is_ignored(live, store):
    await _open_review(live, store)
    assert await handle_reply("   ", live, store=store, engine=FakeEngine()) is None
    assert store.has_pending() is True


# ------------------------------------------------- failures during a review turn
async def test_a_usage_limit_mid_review_keeps_the_review_alive(live, store):
    """The asymmetry the resume contract creates: the transcript is on disk (row 5), so this is
    survivable — the owner just re-sends after the window resets. Nothing is written, nothing lost."""
    engine = FakeEngine(ClaudeResult(text=DRAFT), ClaudeUsageLimit("429 limit reached"))
    await _open_review(live, store, engine)

    reply = await handle_reply("담당자는 김철수", live, store=store, engine=engine)

    assert "사용량 한도" in reply
    assert store.pending().state is ReviewState.AWAITING_REVIEW
    assert store.pending().draft  # the draft is untouched
    assert notes(live) == []


async def test_a_usage_limit_mid_review_never_defers(live, store):
    """DeferMessage here would propagate out of the poller's task, not into catch-up — and halting
    stops the poller, i.e. the only channel the retry could arrive on. Deadlock."""
    engine = FakeEngine(ClaudeResult(text=DRAFT), ClaudeUsageLimit("429"))
    await _open_review(live, store, engine)

    # Must not raise.
    await handle_reply("수정해주세요", live, store=store, engine=engine)


async def test_the_owner_can_retry_after_a_usage_limit(live, store):
    engine = FakeEngine(
        ClaudeResult(text=DRAFT), ClaudeUsageLimit("429"), ClaudeResult(text=REVISED)
    )
    await _open_review(live, store, engine)
    await handle_reply("담당자는 김철수", live, store=store, engine=engine)

    reply = await handle_reply("담당자는 김철수", live, store=store, engine=engine)

    assert "김철수" in reply
    assert store.pending().state is ReviewState.AWAITING_REVIEW


async def test_a_lost_session_delivers_the_draft(live, store):
    """Terminal: there is nothing to resume, ever. So hand the owner what we have rather than
    stranding it — a draft without its review is still a draft."""
    engine = FakeEngine(ClaudeResult(text=DRAFT), ClaudeSessionLost("No conversation found"))
    await _open_review(live, store, engine)

    reply = await handle_reply("담당자는 김철수", live, store=store, engine=engine)

    assert len(notes(live)) == 1
    assert "초안 그대로 저장" in reply
    assert store.has_pending() is False


async def test_a_delivered_draft_is_marked_unreviewed(live, store):
    """It must be impossible to mistake a delivered draft for one the owner accepted."""
    engine = FakeEngine(ClaudeResult(text=DRAFT), ClaudeSessionLost("gone"))
    await _open_review(live, store, engine)
    await handle_reply("수정", live, store=store, engine=engine)

    frontmatter, body = note_data(live)
    assert frontmatter["reviewed"] is False
    assert body.startswith("> ⚠️ 검토를 마치지 못한 초안입니다")
    assert "서버 임대 비용" in body  # …and the draft itself survives intact


async def test_a_transient_error_keeps_the_review_alive(live, store):
    """Not a lost session: the transcript is still there, so retrying can work."""
    engine = FakeEngine(ClaudeResult(text=DRAFT), ClaudeError("engine hiccup"))
    await _open_review(live, store, engine)

    reply = await handle_reply("수정", live, store=store, engine=engine)

    assert "다시 보내주시면" in reply
    assert store.pending().failures == 1
    assert notes(live) == []


async def test_repeated_failures_give_up_and_deliver_the_draft(live, store):
    """The give-up that stops "stay and retry" from stranding a review forever against a
    permanently broken engine — the exact failure the state machine exists to prevent."""
    engine = FakeEngine(
        ClaudeResult(text=DRAFT), ClaudeError("1"), ClaudeError("2"), ClaudeError("3")
    )
    await _open_review(live, store, engine)

    for _ in range(2):
        await handle_reply("수정", live, store=store, engine=engine)
        assert store.has_pending() is True

    reply = await handle_reply("수정", live, store=store, engine=engine)

    assert "초안 그대로 저장" in reply
    assert len(notes(live)) == 1
    assert store.has_pending() is False


async def test_a_successful_turn_resets_the_failure_count(live, store):
    """Failures must be *consecutive*, or a long review would eventually give up on itself."""
    engine = FakeEngine(
        ClaudeResult(text=DRAFT), ClaudeError("blip"), ClaudeResult(text=REVISED)
    )
    await _open_review(live, store, engine)
    await handle_reply("수정", live, store=store, engine=engine)
    await handle_reply("수정", live, store=store, engine=engine)

    assert store.pending().failures == 0


async def test_a_failed_revision_leaves_the_draft_intact(live, store):
    engine = FakeEngine(ClaudeResult(text=DRAFT), ClaudeError("boom"))
    await _open_review(live, store, engine)
    before = store.pending().draft

    await handle_reply("수정", live, store=store, engine=engine)

    assert store.pending().draft == before


# -------------------------------------------------------------------- expiry
async def test_a_fresh_review_does_not_expire(live, store):
    await _open_review(live, store)
    assert await expire_stale(live, store=store) is None
    assert store.has_pending() is True


async def test_an_unanswered_review_expires_into_a_delivered_draft(live, store):
    """An expiry is involuntary, so it delivers. The bound also keeps the review inside the Bot
    API's 24h retention window — past that a reply could not reach us anyway."""
    await _open_review(live, store)
    later = store.pending().created_at + timedelta(hours=25)

    reply = await expire_stale(live, store=store, now=later)

    assert "초안 그대로 저장" in reply
    assert len(notes(live)) == 1
    assert store.has_pending() is False
    frontmatter, _ = note_data(live)
    assert frontmatter["reviewed"] is False


async def test_expiry_with_no_review_is_a_no_op(live, store):
    assert await expire_stale(live, store=store) is None


async def test_the_expiry_window_is_configurable(live, store):
    await _open_review(live, store)
    short = replace(live, review_expiry_hours=1.0)
    later = store.pending().created_at + timedelta(hours=2)

    assert await expire_stale(short, store=store, now=later) is not None
