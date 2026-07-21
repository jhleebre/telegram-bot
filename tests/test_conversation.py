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
from contextbot.handlers.conversation import (
    QUESTIONS_HEADING,
    activate,
    bot_dm_status,
    discard_incomplete,
    expire_stale,
    handle_reply,
    parse_draft,
    promote_next,
    review_block,
    split_tags,
    strip_trailing_rule,
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


def test_a_rule_separates_the_draft_from_the_questions(store):
    """The draft ends (often on the elision notice) and the questions read as their own block: a
    rule between them, mirroring the one above the footer."""
    review = store.create(message_id=7, session_id="s", title="t", source_date=DATE)
    review.write_draft("본문")
    review.questions = "- 담당자는 누구인가요?"

    block = review_block(review)
    # The draft, then a rule, then the questions — the rule sits between the two.
    assert block.index("본문") < block.index("──────────") < block.index("담당자는 누구인가요?")
    # Two rules in all: one framing the top of the questions, one framing the footer below them.
    assert block.count("──────────") == 2


def test_no_rule_hangs_over_the_footer_when_there_are_no_questions(store):
    """With nothing to ask, the questions block is empty — so only the footer's own rule remains,
    and the owner is not shown a separator floating above nothing."""
    review = store.create(message_id=7, session_id="s", title="t", source_date=DATE)
    review.write_draft("본문")
    review.questions = "- (없음)"
    assert review_block(review).count("──────────") == 1


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
    activate(review, store)

    assert discard_incomplete(store) is False
    assert store.has_pending() is True


def test_discard_incomplete_keeps_a_review_whose_draft_is_legitimately_empty(store):
    """The draft *file* is the signal, not its content: a completed turn that produced an empty
    body is still a live review, and the owner has already been asked about it."""
    review = store.create(message_id=7, session_id="s", title="t", source_date=DATE)
    review.write_draft("")
    activate(review, store)

    assert discard_incomplete(store) is False
    assert store.has_pending() is True


def test_discard_incomplete_drops_a_queued_review_that_never_got_a_draft(store):
    """The queue needs the same rule, for the same reason.

    A crash during a *queued* job's draft turn leaves the identical draftless entry — and keeping it
    would hold a place in the queue for a note that does not exist, so the promotion after the
    current review ends would announce a meeting note with nothing in it.
    """
    store.create(message_id=7, session_id="s", title="회의록 작성 중", source_date=DATE)

    assert discard_incomplete(store) is True
    assert store.queued() == []


def test_discard_incomplete_keeps_a_queued_review_that_has_its_draft(store):
    review = store.create(message_id=7, session_id="s", title="t", source_date=DATE)
    review.write_draft("초안")

    assert discard_incomplete(store) is False
    assert [r.message_id for r in store.queued()] == [7]


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


# ----------------------------------------------------------- accepting the draft
async def _open_review(live, store, *, note_type: str = "meeting-note") -> None:
    """A review that has been asked about, in the state a producer leaves it in.

    Built directly rather than through `handle_audio`, on purpose. Increment 4's version of this
    called `handle_review_request` — `#검토` was the producer, and it was cheap. Audio is not: going
    through it would drag Whisper, a download and a staging dir into every test of *cancelling*.
    The producer→loop seam is asserted where it belongs, in `test_audio_handler.py`; from here down
    the only thing that matters is that a draft exists and the owner has been asked about it.
    """
    review = store.create(
        message_id=7, session_id="sess-1", title="3분기 인프라 예산 회의",
        source_date=DATE, note_type=note_type,
    )
    title, body, questions = parse_draft(DRAFT)
    review.title = title
    review.questions = questions
    review.write_draft(body)
    activate(review, store)


@pytest.mark.parametrize("word", ["확인", "ok", "OK", "네", "저장", "확인!", "확인.", "ok!"])
async def test_accepting_writes_the_note_and_ends_the_review(live, store, word):
    await _open_review(live, store)
    engine = FakeEngine()

    reply = await handle_reply(word, live, store=store, engine=engine)

    assert "저장됨" in reply
    assert len(notes(live)) == 1
    assert store.has_pending() is False
    # The *note* still needs no engine call — the draft on disk is what the owner just approved.
    # The one call here is the glossary turn, which is best-effort and cannot cost them the note
    # (test_a_failed_glossary_turn_never_costs_the_owner_their_note).
    assert len(engine.calls) == 1


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
    engine = FakeEngine(ClaudeResult(text=REVISED))
    await _open_review(live, store)
    session_id = store.pending().session_id

    reply = await handle_reply("담당자는 김철수, 기한은 7월 24일", live, store=store, engine=engine)

    assert engine.calls[0]["resume"] == session_id
    assert engine.calls[0]["cwd"] == store.pending().work_dir
    assert "김철수" in reply
    assert "김철수" in store.pending().draft


async def test_the_correction_text_reaches_the_prompt(live, store):
    engine = FakeEngine(ClaudeResult(text=REVISED))
    await _open_review(live, store)

    await handle_reply("담당자는 김철수", live, store=store, engine=engine)
    assert "담당자는 김철수" in engine.calls[0]["prompt"]


async def test_a_revision_loops_back_to_awaiting_not_to_a_note(live, store):
    """The owner reviews a revision before it becomes a note — a correction is not an acceptance."""
    engine = FakeEngine(ClaudeResult(text=REVISED))
    await _open_review(live, store)

    await handle_reply("담당자는 김철수", live, store=store, engine=engine)

    assert notes(live) == []
    assert store.pending().state is ReviewState.AWAITING_REVIEW


async def test_the_review_can_be_accepted_after_a_revision(live, store):
    engine = FakeEngine(ClaudeResult(text=REVISED))
    await _open_review(live, store)
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
    engine = FakeEngine(ClaudeUsageLimit("429 limit reached"))
    await _open_review(live, store)

    reply = await handle_reply("담당자는 김철수", live, store=store, engine=engine)

    assert "사용량 한도" in reply
    assert store.pending().state is ReviewState.AWAITING_REVIEW
    assert store.pending().draft  # the draft is untouched
    assert notes(live) == []


async def test_a_usage_limit_mid_review_never_defers(live, store):
    """DeferMessage here would propagate out of the poller's task, not into catch-up — and halting
    stops the poller, i.e. the only channel the retry could arrive on. Deadlock."""
    engine = FakeEngine(ClaudeUsageLimit("429"))
    await _open_review(live, store)

    # Must not raise.
    await handle_reply("수정해주세요", live, store=store, engine=engine)


async def test_the_owner_can_retry_after_a_usage_limit(live, store):
    engine = FakeEngine(
        ClaudeUsageLimit("429"), ClaudeResult(text=REVISED)
    )
    await _open_review(live, store)
    await handle_reply("담당자는 김철수", live, store=store, engine=engine)

    reply = await handle_reply("담당자는 김철수", live, store=store, engine=engine)

    assert "김철수" in reply
    assert store.pending().state is ReviewState.AWAITING_REVIEW


async def test_a_lost_session_delivers_the_draft(live, store):
    """Terminal: there is nothing to resume, ever. So hand the owner what we have rather than
    stranding it — a draft without its review is still a draft."""
    engine = FakeEngine(ClaudeSessionLost("No conversation found"))
    await _open_review(live, store)

    reply = await handle_reply("담당자는 김철수", live, store=store, engine=engine)

    assert len(notes(live)) == 1
    assert "초안 그대로 저장" in reply
    assert store.has_pending() is False


async def test_a_delivered_draft_is_marked_unreviewed(live, store):
    """It must be impossible to mistake a delivered draft for one the owner accepted."""
    engine = FakeEngine(ClaudeSessionLost("gone"))
    await _open_review(live, store)
    await handle_reply("수정", live, store=store, engine=engine)

    frontmatter, body = note_data(live)
    assert frontmatter["reviewed"] is False
    assert body.startswith("> ⚠️ 검토를 마치지 못한 초안입니다")
    assert "서버 임대 비용" in body  # …and the draft itself survives intact


async def test_a_transient_error_keeps_the_review_alive(live, store):
    """Not a lost session: the transcript is still there, so retrying can work."""
    engine = FakeEngine(ClaudeError("engine hiccup"))
    await _open_review(live, store)

    reply = await handle_reply("수정", live, store=store, engine=engine)

    assert "다시 보내주시면" in reply
    assert store.pending().failures == 1
    assert notes(live) == []


async def test_repeated_failures_give_up_and_deliver_the_draft(live, store):
    """The give-up that stops "stay and retry" from stranding a review forever against a
    permanently broken engine — the exact failure the state machine exists to prevent."""
    engine = FakeEngine(
        ClaudeError("1"), ClaudeError("2"), ClaudeError("3")
    )
    await _open_review(live, store)

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
        ClaudeError("blip"), ClaudeResult(text=REVISED)
    )
    await _open_review(live, store)
    await handle_reply("수정", live, store=store, engine=engine)
    await handle_reply("수정", live, store=store, engine=engine)

    assert store.pending().failures == 0


async def test_a_failed_revision_leaves_the_draft_intact(live, store):
    engine = FakeEngine(ClaudeError("boom"))
    await _open_review(live, store)
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


# ------------------------------------------------- increment 5: tags in the draft
def test_split_tags_pulls_the_line_off_the_body():
    tags, body = split_tags("태그: 인프라, 예산, 3분기-계획\n\n## Overview\n\n내용")

    assert tags == ["인프라", "예산", "3분기-계획"]
    assert body == "## Overview\n\n내용"


def test_split_tags_tolerates_a_body_without_one():
    """A memo review has no tags line, and a model that forgets it has not failed."""
    assert split_tags("## Overview\n\n내용") == ([], "## Overview\n\n내용")


def test_split_tags_strips_a_stray_hash():
    assert split_tags("태그: #에이닷, #B2B\n\n본문")[0] == ["에이닷", "B2B"]


def test_split_tags_ignores_empty_entries():
    assert split_tags("태그: 인프라, , 예산\n\n본문")[0] == ["인프라", "예산"]


async def test_a_revision_does_not_leave_the_tags_line_in_the_note_body(live, store):
    """The body-corruption bug, pre-empted.

    `review_revise.md` asks for the note "in the same structure as before", so a revision of a
    meeting note reproduces its `태그:` line — and without the strip, that line lands as the note
    body's first line. Nothing raises; the note is just quietly wrong.
    """
    await _open_review(live, store)
    review = store.pending()
    review.note_type = "meeting-note"
    store.update(review)
    revised = f"제목: 회의\n태그: 인프라, 예산\n\n## Overview\n\n본문입니다.\n\n{QUESTIONS_HEADING}\n\n- (없음)\n"

    await handle_reply("고쳐주세요", live, store=store, engine=FakeEngine(ClaudeResult(text=revised)))

    assert store.pending().draft.startswith("## Overview")
    assert "태그:" not in store.pending().draft
    assert store.pending().tags == ["인프라", "예산"]


# --------------------------------------------------------- increment 5: the queue
def _queued(store, message_id: int, *, title: str = "대기 중인 회의록"):
    review = store.create(
        message_id=message_id, session_id=f"s-{message_id}", title=title, source_date=DATE,
        note_type="meeting-note",
    )
    review.write_draft("## Overview\n\n대기 중인 초안입니다.")
    store.update(review)
    return review


async def test_promote_next_asks_about_the_oldest_queued_review(live, store):
    _queued(store, 8, title="첫 번째 회의")
    _queued(store, 9, title="두 번째 회의")

    reply = promote_next(store)

    assert "첫 번째 회의" in reply
    assert store.pending().message_id == 8
    assert [r.message_id for r in store.queued()] == [9]


def test_promote_next_says_how_many_are_still_waiting(store):
    _queued(store, 8)
    _queued(store, 9)

    assert "뒤에 1건 더 대기" in promote_next(store)


def test_promote_next_does_nothing_while_a_review_is_open(live, store):
    review = store.create(message_id=7, session_id="s", title="진행 중", source_date=DATE)
    review.write_draft("초안")
    activate(review, store)
    _queued(store, 8)

    assert promote_next(store) is None
    assert store.pending().message_id == 7


def test_promote_next_with_an_empty_queue_is_a_no_op(store):
    assert promote_next(store) is None


def test_promotion_starts_the_clock_when_the_owner_is_asked(store):
    """A queued review may wait an hour behind another one.

    `created_at` gates the poller's backlog filter, so it has to mean "when they were asked" — not
    when the recording arrived, and not when the draft was made. Stamped any earlier, anything the
    owner typed at the bot while waiting would sail through the filter and be applied to this draft
    as a correction.
    """
    review = _queued(store, 8)
    stamped_at_create = review.created_at

    promote_next(store)

    assert store.pending().created_at > stamped_at_create


def test_promotion_makes_no_engine_call(store):
    """The property the whole queue design rests on: promotion cannot fail.

    It runs from a poller background task where there is no handler to replay and no DeferMessage
    to raise, so an engine call here would need its own retry driver and its own give-up counter.
    The draft already exists; promotion is a timestamp and a message. (Enforced by signature: there
    is no engine to pass it.)
    """
    _queued(store, 8)

    reply = promote_next(store)

    assert reply is not None
    assert store.pending().draft.startswith("## Overview")


# ------------------------------- increment 5: what accepting means for a meeting
GLOSSARY_REPLY = """| 전사 표현 | 정확한 표현 | 유형 | 설명 |
|---|---|---|---|
| 팀웹 | T-map | 제품명 | SKT 내비게이션 |
"""


async def _open_meeting(live, store, *, message_id: int = 7):
    """A meeting review, asked about, with its audio staged where a real one would put it."""
    review = store.create(
        message_id=message_id, session_id="sess-m", title="3분기 회의", source_date=DATE,
        note_type="meeting-note",
    )
    review.write_draft("## Overview\n\n예산을 10-20% 줄이기로 했다.")
    review.tags = ["인프라", "예산"]
    review.audio_dir.mkdir(parents=True, exist_ok=True)
    (review.audio_dir / "meeting.m4a").write_bytes(b"audio")
    activate(review, store)
    return review


async def test_accepting_a_meeting_files_the_confirmed_terms(live, store):
    review = await _open_meeting(live, store)
    engine = FakeEngine(ClaudeResult(text=GLOSSARY_REPLY))

    reply = await handle_reply("확인", live, store=store, engine=engine)

    assert "저장됨" in reply
    assert "팀웹 → T-map" in reply
    assert "| 팀웹 | T-map | 제품명 | SKT 내비게이션 |" in live.glossary_path.read_text(encoding="utf-8")


async def test_the_glossary_turn_resumes_the_same_session(live, store):
    """Only the conversation knows which corrections the owner actually confirmed."""
    review = await _open_meeting(live, store)
    engine = FakeEngine(ClaudeResult(text="NONE"))

    await handle_reply("확인", live, store=store, engine=engine)

    assert engine.calls[0]["resume"] == "sess-m"


async def test_accepting_writes_the_meeting_note_with_its_type_and_tags(live, store):
    await _open_meeting(live, store)

    await handle_reply("확인", live, store=store, engine=FakeEngine(ClaudeResult(text="NONE")))

    frontmatter, _ = note_data(live)
    assert frontmatter["type"] == "meeting-note"
    assert frontmatter["tags"] == ["인프라", "예산"]
    assert frontmatter["reviewed"] is True


async def test_accepting_deletes_the_audio(live, store):
    """"Success" now means the recording is gone — and it happens for free, because the audio lives
    in the review's tree and ending a review drops the tree. Nothing has to remember to do it."""
    review = await _open_meeting(live, store)
    assert (review.audio_dir / "meeting.m4a").is_file()

    await handle_reply("확인", live, store=store, engine=FakeEngine(ClaudeResult(text="NONE")))

    assert not review.audio_dir.exists()
    assert not review.review_dir.exists()


async def test_cancelling_deletes_the_audio_too(live, store):
    review = await _open_meeting(live, store)

    await handle_reply("취소", live, store=store, engine=FakeEngine())

    assert not review.review_dir.exists()


async def test_a_delivered_draft_deletes_the_audio_too(live, store):
    """Every involuntary end drops the tree as well — expiry included."""
    review = await _open_meeting(live, store)
    later = store.pending().created_at + timedelta(hours=25)

    await expire_stale(live, store=store, now=later)

    assert not review.review_dir.exists()


async def test_a_failed_glossary_turn_never_costs_the_owner_their_note(live, store):
    """They said 확인. The note is the deliverable; the glossary is a footnote.

    A usage limit, a lost session, a timeout — every reason to be here is survivable, and none of
    them is a reason to withhold the note the owner just approved.
    """
    await _open_meeting(live, store)
    engine = FakeEngine(ClaudeUsageLimit("5-hour limit reached"))

    reply = await handle_reply("확인", live, store=store, engine=engine)

    assert "저장됨" in reply
    assert "용어집은 갱신하지 못했습니다" in reply
    assert len(notes(live)) == 1
    assert note_data(live)[0]["reviewed"] is True
    assert store.has_pending() is False


async def test_a_lost_session_at_accept_still_writes_the_note(live, store):
    await _open_meeting(live, store)
    engine = FakeEngine(ClaudeSessionLost("No conversation found with session ID: sess-m"))

    reply = await handle_reply("확인", live, store=store, engine=engine)

    assert "저장됨" in reply
    assert note_data(live)[0]["reviewed"] is True


async def test_the_glossary_is_only_appended_after_the_note_is_written(live, store):
    """Every side effect after the last LLM call — the rule's last application, at the end of a
    review rather than the start of a job."""
    await _open_meeting(live, store)
    engine = FakeEngine(ClaudeResult(text=GLOSSARY_REPLY))

    await handle_reply("확인", live, store=store, engine=engine)

    assert len(notes(live)) == 1
    assert live.glossary_path.is_file()


async def test_an_empty_glossary_answer_adds_nothing(live, store):
    """A clean transcript teaches nothing, and that is a normal outcome."""
    await _open_meeting(live, store)

    reply = await handle_reply("확인", live, store=store, engine=FakeEngine(ClaudeResult(text="NONE")))

    assert "용어집" not in reply
    assert not live.glossary_path.exists()


async def test_a_non_meeting_review_makes_no_glossary_call(live, store):
    """Only a transcript can teach the recogniser anything.

    Audio is the only producer today, so every real review is a meeting note — but the branch is
    what keeps that from being an assumption. It guards the next producer (open decision 6: a text
    review started from the bot DM), which will have no transcript behind it and nothing to file.
    """
    await _open_review(live, store, note_type="note")
    engine = FakeEngine()

    await handle_reply("확인", live, store=store, engine=engine)

    assert engine.calls == []
    assert note_data(live)[0]["type"] == "note"


async def test_a_duplicate_term_is_not_appended_twice(live, store):
    live.glossary_path.write_text(
        "## 용어 목록\n\n| 전사 표현 | 정확한 표현 | 유형 | 설명 |\n|---|---|---|---|\n"
        "| 팀웹 | T-map | 제품명 | 기존 |\n",
        encoding="utf-8",
    )
    await _open_meeting(live, store)

    reply = await handle_reply(
        "확인", live, store=store, engine=FakeEngine(ClaudeResult(text=GLOSSARY_REPLY))
    )

    assert "용어집에 추가" not in reply
    assert live.glossary_path.read_text(encoding="utf-8").count("팀웹") == 1


def test_strip_trailing_rule_drops_a_separator_left_before_the_questions():
    """Found on the first real meeting-note run: the model put `---` before the questions heading,
    `parse_draft` split on the heading, and the rule stayed in the note body as a dangling <hr>."""
    assert strip_trailing_rule("## Notes\n\n내용입니다.\n\n---") == "## Notes\n\n내용입니다."
    assert strip_trailing_rule("본문\n\n***\n") == "본문"
    assert strip_trailing_rule("본문\n\n___") == "본문"


def test_strip_trailing_rule_keeps_real_content():
    assert strip_trailing_rule("## Notes\n\n내용입니다.") == "## Notes\n\n내용입니다."
    # A list item is not a rule, and neither is a table.
    assert strip_trailing_rule("- 항목 하나\n- 항목 둘") == "- 항목 하나\n- 항목 둘"
    assert strip_trailing_rule("| a | b |\n|---|---|\n| 1 | 2 |").endswith("| 1 | 2 |")


def test_strip_trailing_rule_only_touches_the_end():
    """A rule inside the body is the author's — including a setext `---` underline, which this
    would otherwise demote from a heading to a paragraph. Only a *trailing* one is the model's
    leftover separator."""
    assert strip_trailing_rule("제목\n---\n\n본문") == "제목\n---\n\n본문"
    assert strip_trailing_rule("## A\n\n하나\n\n---\n\n## B\n\n둘") == "## A\n\n하나\n\n---\n\n## B\n\n둘"


# --------------------------------- increment 5: the preamble (found on a real run)
PREAMBLE_DRAFT = f"""글로시리 확인이 완료됐습니다. 전사에서 「팀웹」→ T-map 치환을 적용하고 노트를 작성합니다.

---

제목: T-map 안건 배분 및 예산 감축 결정
태그: T-map, 예산-감축

## Overview

내용입니다.

{QUESTIONS_HEADING}

- (없음)
"""


def test_parse_draft_survives_a_preamble_before_the_title():
    """Measured, not hypothetical.

    Both prompts say "no preamble" and it mostly works — the first real meeting-note run obeyed.
    The second opened with a sentence about its own glossary work, and that one slip cost
    everything at once: the title fell back to the filename, the tags came out empty, and the
    preamble *plus the raw 제목:/태그: lines* were saved as the note's body. Nothing raised.
    """
    title, body, questions = parse_draft(PREAMBLE_DRAFT)

    assert title == "T-map 안건 배분 및 예산 감축 결정"
    assert body.startswith("태그: T-map")  # the tags line is split_tags' job, not this one
    assert "글로시리 확인이 완료됐습니다" not in body
    assert "(없음)" in questions


def test_a_preamble_does_not_cost_the_tags_either(live, store):
    tags, body = split_tags(parse_draft(PREAMBLE_DRAFT)[1])

    assert tags == ["T-map", "예산-감축"]
    assert body == "## Overview\n\n내용입니다."


def test_parse_draft_leaves_a_body_with_no_title_line_alone():
    """No 제목: anywhere means there is nothing to strip — never discard a real body hunting one."""
    title, body, _ = parse_draft("## Overview\n\n내용입니다.")

    assert title is None
    assert body == "## Overview\n\n내용입니다."


def test_a_title_line_deep_in_the_body_is_not_treated_as_a_preamble_marker():
    """The bound matters: a preamble is an opening remark, so a `제목:` further down is content and
    must never eat the note above it."""
    body_lines = "\n".join(f"## 섹션 {i}\n\n내용" for i in range(6))
    text = f"{body_lines}\n\n제목: 이건 본문 안의 글자입니다"

    _, body, _ = parse_draft(text)

    assert body.startswith("## 섹션 0")
    assert "제목: 이건 본문 안의 글자입니다" in body


async def test_a_preamble_on_a_revision_does_not_corrupt_the_note(live, store):
    """The revise turn shares the parsing, so it shares the protection."""
    await _open_review(live, store)
    revised = f"알겠습니다. 수정했습니다.\n\n제목: 고친 제목\n\n## 결정사항\n\n고친 내용.\n\n{QUESTIONS_HEADING}\n\n- (없음)\n"

    await handle_reply("고쳐주세요", live, store=store, engine=FakeEngine(ClaudeResult(text=revised)))

    assert store.pending().title == "고친 제목"
    assert store.pending().draft == "## 결정사항\n\n고친 내용."


async def test_promotion_never_asks_about_a_draft_that_does_not_exist_yet(live, store):
    """The race the queue creates, and it is entirely plausible.

    A producer creates its entry QUEUED and *then* spends minutes drafting. If the owner answers the
    open review during that window — reading it on their phone while the second recording is still
    transcribing — the review ends and promotion fires against a review whose turn is still in
    flight. Both halves lose: the owner gets "초안이 준비됐습니다" with an empty body, and the
    producer then writes its own object back over the activation, leaving a finished draft QUEUED
    with nothing pending. The poller stops, and nobody is asked until the next restart.
    """
    store.create(message_id=8, session_id="s-8", title="회의록 작성 중", source_date=DATE)  # mid-draft

    assert promote_next(store) is None
    assert store.pending() is None


async def test_promotion_skips_the_in_flight_one_and_takes_the_ready_one(store):
    store.create(message_id=8, session_id="s-8", title="아직 작성 중", source_date=DATE)
    ready = store.create(message_id=9, session_id="s-9", title="준비된 회의록", source_date=DATE)
    ready.write_draft("## Overview\n\n초안입니다.")
    store.update(ready)

    reply = promote_next(store)

    assert "준비된 회의록" in reply
    assert store.pending().message_id == 9
    assert "뒤에" not in reply  # the in-flight one is not "waiting", it is unfinished


# ------------------------------------- talking to the bot when no review wants it
def test_the_status_reply_says_it_is_running_and_where_notes_go(store):
    """Two jobs: prove the bot is alive, and point at the channel that actually captures. The bot
    DM is not the capture channel, and a memo typed here would otherwise vanish."""
    text = bot_dm_status(store)

    assert "실행 중" in text
    assert "검토 중인 초안이 없습니다" in text
    assert "Saved Messages" in text


def test_the_status_reply_names_the_draft_it_is_holding(store):
    review = store.create(message_id=7, session_id="s", title="3분기 예산 회의", source_date=DATE)
    review.write_draft("초안")
    activate(review, store)

    text = bot_dm_status(store)

    assert "3분기 예산 회의" in text
    assert "확인" in text and "취소" in text


def test_a_stale_message_is_told_why_it_was_not_applied(store):
    """A bare status here would look like the answer had been ignored — and the owner very likely
    typed `확인` at a question they had not yet been shown."""
    review = store.create(message_id=7, session_id="s", title="3분기 예산 회의", source_date=DATE)
    review.write_draft("초안")
    activate(review, store)

    text = bot_dm_status(store, stale=True)

    assert "반영하지 않았습니다" in text
    assert "3분기 예산 회의" in text, "…and it still says what is actually waiting"


def test_a_stale_message_with_no_review_is_just_a_status(store):
    """Nothing to be late for: the review it would have predated does not exist."""
    assert "반영하지 않았습니다" not in bot_dm_status(store, stale=True)


def test_the_status_reply_carries_no_markdown(store):
    """`Notifier.send` passes no parse_mode, so Telegram renders replies literally — `**bold**`
    would arrive as asterisks. Turning parse_mode on would be worse than untidy: note filenames are
    full of underscores, which Markdown reads as italics, and a parse error makes Telegram reject
    the message — which Notifier *swallows*, so a confirmation would vanish rather than fail."""
    review = store.create(message_id=7, session_id="s", title="회의", source_date=DATE)
    review.write_draft("초안")
    activate(review, store)

    for text in (bot_dm_status(store), bot_dm_status(store, stale=True)):
        assert "**" not in text, f"asterisks arrive as asterisks: {text!r}"
