"""The human-in-the-loop review loop: draft → ask → revise → accept.

This is increment 4's plumbing (docs/PHASE2.md). It owns a review's whole lifecycle — starting one,
routing the owner's bot-DM reply into it, and every way it can end — so increment 5's audio pipeline
adds a *producer* of reviews rather than a second copy of the machinery.

**The simple case it is built on is a memo prefixed `#검토`.** The delivery approach says to build
the state machine on something cheap before the audio pipeline depends on it, and a memo is exactly
that: no Whisper, no download, no staging, but the same multi-turn resume contract end-to-end. A
plain memo is untouched — this route is opt-in.

The rules the increment's open decision settled (recorded in full in docs/PHASE2.md):

- **A review turn never raises `DeferMessage`.** There is no handler on the stack to replay, replay
  would re-run the whole job over a draft that already exists, and halting stops the poller — i.e.
  the very channel the reply must arrive on. `DeferMessage` belongs to capture (turn 1) only.
- **A review never ends empty-handed.** A lost session, a review that cannot make progress, and an
  expired one all end by *delivering the draft* as a note marked unreviewed. Only `취소` discards,
  because the intent is unambiguous and Saved Messages still holds the input.
- **A usage limit mid-review keeps the review alive.** The transcript is on disk and resumes after
  the window resets, so the owner just re-sends their reply. Nothing is written, nothing is lost.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings
from ..core.session_store import PendingReview, ReviewState, SessionStore, build_store
from ..engine import prompts
from ..engine.claude_cli import (
    ClaudeCLI,
    ClaudeError,
    ClaudeSessionLost,
    ClaudeUsageLimit,
    build_engine,
)
from ..notes.markdown_writer import write_note
from .base import DeferMessage, HandlerResult, IncomingMessage
from .text_handler import clean_title

logger = logging.getLogger("contextbot.handlers.conversation")

# The opt-in trigger. Narrow on purpose: a memo without it takes the shipped one-shot path.
REVIEW_PREFIX = "#검토"

QUESTIONS_HEADING = "## 확인 요청"
_SENTINEL = "DRAFT_FAILED"
_MIN_OUTPUT_CHARS = 20
_REVIEW_TIMEOUT_SEC = 120.0

# Give up after this many consecutive failed turns on one review and deliver the draft. Without it,
# "stay in AWAITING_REVIEW and let the owner retry" would strand a review forever against a
# permanently broken engine — the exact failure the state machine exists to prevent.
_MAX_FAILURES = 3

# Telegram rejects a message over 4096 characters, and Notifier swallows the rejection — so an
# oversized review block is one the owner never sees while the store still waits for their answer:
# an unanswerable review. The draft is on disk; the DM is only a preview, so it is safe to cut.
# The budget is the *whole block*, not just the draft: the title, the questions (a model that
# ignores "two to four" can produce a lot of them), and the footer all ride along, and bounding
# only the draft leaves the total unbounded — which is the same bug with more steps.
_TELEGRAM_MAX_CHARS = 4096
_MAX_DM_CHARS = 3500
_ELIDED = "\n…(생략 — 전체 내용은 저장할 때 노트에 들어갑니다)"

_ACCEPT = {"확인", "ok", "okay", "예", "네", "저장", "좋아요", "승인"}
_CANCEL = {"취소", "cancel", "그만", "중단"}

_SYSTEM_PROMPT = (
    "You are a note editor for a personal knowledge base. You organise what the author gives you "
    "and ask about what you had to guess. You never invent content you were not given."
)


# --------------------------------------------------------------------------- parsing
def is_review_request(text: str) -> bool:
    """True when a memo opts into the review loop."""
    return text.strip().startswith(REVIEW_PREFIX)


def strip_prefix(text: str) -> str:
    return text.strip()[len(REVIEW_PREFIX) :].strip()


def _draft_failed(text: str) -> bool:
    """True when a turn produced nothing usable.

    The sentinel is matched on the **first line** rather than by substring — the same guard the
    PDF and image routes need, for the same reason: a memo may legitimately contain the token.
    """
    stripped = text.strip()
    if len(stripped) < _MIN_OUTPUT_CHARS:
        return True
    return stripped.splitlines()[0].strip().strip("`*# ").startswith(_SENTINEL)


def parse_draft(text: str) -> tuple[str | None, str, str]:
    """Split a turn's output into ``(title, body, questions)``.

    Every part is optional-tolerant on purpose: a missing title falls back, and a missing questions
    heading just means there is nothing to ask. A malformed reply must never cost the owner a draft
    the model actually produced.
    """
    stripped = text.strip()
    title: str | None = None
    first, _, rest = stripped.partition("\n")
    if first.strip().startswith("제목:"):
        title = clean_title(first.strip()[len("제목:") :])
        stripped = rest.strip()

    body, sep, questions = stripped.partition(QUESTIONS_HEADING)
    if not sep:
        return title, stripped, ""
    return title, body.strip(), questions.strip()


def _has_questions(questions: str) -> bool:
    """False when the model said there is nothing to ask (`- (없음)`) or asked nothing."""
    text = questions.strip()
    return bool(text) and text.replace("-", "").strip() != "(없음)"


def _elide(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, saying so."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + _ELIDED


_FOOTER = (
    "──────────\n"
    "이대로 저장하려면 `확인`, 버리려면 `취소`.\n"
    "고칠 부분이 있으면 그냥 알려주세요 — 반영해서 다시 보여드립니다."
)


def review_block(review: PendingReview) -> str:
    """The bot-DM message that asks the owner to review a draft. Always deliverable.

    The questions are what the owner must actually answer, so they are kept whole and the draft
    gives up whatever room they need — the draft is the part they can read in the note.
    """
    header = f"📝 초안이 준비됐습니다 — 「{review.title}」"
    questions = review.questions if _has_questions(review.questions) else ""

    # Everything except the draft is fixed cost; the draft takes what is left of the budget.
    fixed = len("\n\n".join(part for part in [header, "", questions, _FOOTER] if part))
    draft = _elide(review.draft, max(_MAX_DM_CHARS - fixed, 0))

    block = "\n\n".join(part for part in [header, draft, questions, _FOOTER] if part)
    # Last resort: even the fixed parts can overrun if the model ignored "two to four questions".
    # A truncated block the owner can answer beats a perfect one Telegram refuses to deliver.
    return _elide(block, _TELEGRAM_MAX_CHARS - len(_ELIDED))


# ------------------------------------------------------------------- writing notes
def _write(review: PendingReview, settings: Settings, *, unreviewed: str | None = None) -> Path:
    """Write the draft as a note. The only side effect in this module, and always last.

    ``unreviewed`` marks a draft the owner never accepted, so it can never be mistaken for one
    they did.
    """
    body = review.draft
    if unreviewed:
        body = (
            f"> ⚠️ 검토를 마치지 못한 초안입니다 — {unreviewed}\n"
            f"> 내용을 그대로 확인해주세요.\n\n{body}"
        )
    return write_note(
        inbox_dir=settings.inbox_dir,
        body=body,
        title=review.title,
        when=review.source_date,
        source="telegram",
        note_type="note",
        tags=[],
        extra={"telegram_message_id": review.message_id, "reviewed": unreviewed is None},
        slug_source=review.title,
    )


def deliver_draft(review: PendingReview, settings: Settings, store: SessionStore, reason: str) -> str:
    """End an unrecoverable review by handing the owner the draft. Never discards.

    Used by every *involuntary* end — a lost session, a review that cannot make progress, an
    expired one. A draft is the expensive artifact (for audio: a Whisper run plus an LLM pass), and
    losing the review's polish is not a reason to lose the work.
    """
    path = _write(review, settings, unreviewed=reason)
    store.remove(review.message_id)
    logger.info("delivered unreviewed draft for %s: %s (%s)", review.message_id, path.name, reason)
    return (
        f"⚠️ 검토를 이어가지 못해 초안 그대로 저장했습니다: {path.name}\n"
        f"원인: {reason}\n"
        "노트 맨 위에 미검토 표시를 넣어뒀습니다."
    )


def discard_incomplete(store: SessionStore) -> bool:
    """Recover a review from a crash. Run at startup. Returns True if one was dropped.

    A turn is not atomic, so the app can die in the middle of one and leave state on disk that
    cannot be true in a fresh process. There are exactly two such states, and they want opposite
    treatment:

    **Turn 1 never finished → drop the review.** The store entry is written *before* the first call
    (so a resume handle always exists), which means a crash there leaves a review with no draft —
    the owner has seen no question, and there is nothing to deliver. Keeping it would be strictly
    worse than dropping it: it would block every later review on the one-at-a-time guard while the
    poller waited for an answer to a question that was never asked. And dropping it loses nothing —
    ``_process`` advances the HWM only *after* the handler returns, so the memo is still unprocessed
    in Saved Messages and catch-up replays it on the next start, which is the very moment this frees
    the guard for that replay. The draft *file* is the signal rather than the draft's content: only
    a completed turn creates it, so a model that legitimately produced an empty body still leaves a
    live review.

    **A later turn never finished → reset FINALIZING, keep the review.** That flag guards against
    two CLI processes resuming one transcript, so it must not outlive the process that needed it:
    no turn can be in flight in a fresh one. Left set, it would answer every reply — including
    ``확인`` and ``취소`` — with "잠시 후 다시 보내주세요" until the expiry fired, so the owner could
    not even rescue their own draft. The draft is intact, so the review is resumable; only the
    in-flight claim is stale.
    """
    review = store.pending()
    if review is None:
        return False

    if not review.draft_path.exists():
        logger.info("discarding review %s: its first turn never finished", review.message_id)
        store.remove(review.message_id)
        return True

    if review.state is ReviewState.FINALIZING:
        logger.info("review %s: clearing a turn that died with the app", review.message_id)
        review.state = ReviewState.AWAITING_REVIEW
        store.update(review)
    return False


# ------------------------------------------------------------------ starting one
async def handle_review_request(
    message: IncomingMessage,
    settings: Settings,
    *,
    engine: ClaudeCLI | None = None,
    store: SessionStore | None = None,
) -> HandlerResult:
    """`#검토 <memo>` → draft it, ask about it, and wait for the owner in the bot DM."""
    store = store or build_store(settings)
    text = strip_prefix(message.text)
    if not text:
        return HandlerResult(reply="검토할 내용이 없습니다 — `#검토` 뒤에 메모를 적어주세요.")

    if not settings.claude_enabled:
        # No engine means no draft, and a review of nothing is nothing. Save the memo as a plain
        # note rather than losing it: the capture is the point, the review is the upgrade.
        return _plain_note(message, settings, text, "Claude 엔진이 꺼져 있습니다 (CLAUDE_ENABLED=false)")

    if store.has_pending():
        # One review at a time (see SessionStore). Reply and advance rather than defer: deferring
        # would halt the bot, which stops the poller and leaves the open review unanswerable —
        # a deadlock. The memo stays in Saved Messages, so re-sending it costs nothing.
        return HandlerResult(
            reply="🔒 이미 검토 중인 초안이 있습니다.\n"
            "그 검토를 먼저 끝낸 뒤 이 메모를 다시 보내주세요."
        )

    # Pin the session id and record the handle *before* the call that creates the session, so a
    # crash mid-call leaves something resumable rather than an orphan (measured: --session-id is
    # honoured, and resuming does not fork the id).
    review = store.create(
        message_id=message.message_id,
        session_id=str(uuid.uuid4()),
        title="검토 중",
        source_date=message.date,
    )

    engine = engine or build_engine(settings)
    try:
        result = await engine.run(
            prompts.render(
                "review_draft",
                text=text,
                sentinel=_SENTINEL,
                questions_heading=QUESTIONS_HEADING,
            ),
            system_prompt=_SYSTEM_PROMPT,
            session_id=review.session_id,
            add_dirs=[review.work_dir],
            cwd=review.work_dir,
            timeout_sec=max(settings.claude_timeout_sec, _REVIEW_TIMEOUT_SEC),
        )
        if _draft_failed(result.text):
            raise ClaudeError("초안을 만들지 못했습니다")
    except ClaudeUsageLimit as exc:
        # Turn 1 *is* a capture, so the usage-limit policy applies here in full. Unwind the store
        # entry first: this is a deliberate abort, and a replay that found a live review would
        # bounce off the one-at-a-time guard above. (A *crash* leaves the entry — that is the case
        # it exists for.)
        store.remove(review.message_id)
        logger.warning("usage limit; deferring message %s: %s", message.message_id, exc)
        raise DeferMessage(str(exc)) from exc
    except ClaudeError as exc:
        store.remove(review.message_id)
        logger.warning("draft failed for message %s: %s", message.message_id, exc)
        return _plain_note(message, settings, text, str(exc))
    except Exception:
        # Anything unforeseen (a broken template, a bug) must not leave a half-open review behind:
        # it would block every later one on the guard above, and the poller would sit waiting on a
        # draft that does not exist. We are still alive here, so we can unwind. A *crash* cannot,
        # which is what discard_incomplete cleans up at the next start.
        store.remove(review.message_id)
        raise

    title, body, questions = parse_draft(result.text)
    review.title = title or "검토 노트"
    review.questions = questions
    review.write_draft(body)
    store.update(review)

    return HandlerResult(reply=review_block(review))


def _plain_note(message: IncomingMessage, settings: Settings, text: str, reason: str) -> HandlerResult:
    """No draft is possible → save the memo as an ordinary note. Never lose the capture."""
    path = write_note(
        inbox_dir=settings.inbox_dir,
        body=text,
        title=clean_title(text.splitlines()[0]) or "메모",
        when=message.date,
        source="telegram",
        note_type="note",
        tags=[],
        extra={"telegram_message_id": message.message_id},
    )
    return HandlerResult(
        reply=f"📝 저장됨: {path.name}\n⚠️ 검토 없이 저장했습니다 — {reason}", saved_path=path
    )


# ------------------------------------------------------------ the owner's replies
async def handle_reply(
    text: str,
    settings: Settings,
    *,
    store: SessionStore,
    engine: ClaudeCLI | None = None,
) -> str | None:
    """Route one bot-DM reply into the active review. Returns what to say back, or None.

    None means "not for us" — no review is open, or the message is empty. The poller only runs
    while a review is open, but an update can still race the end of one.
    """
    text = text.strip()
    review = store.pending()
    if review is None or not text:
        return None

    if review.state is ReviewState.FINALIZING:
        # A turn is already in flight. Resuming the same session twice would put two CLI processes
        # on one transcript; make the owner wait instead.
        return "⏳ 아직 이전 답장을 반영하는 중입니다 — 잠시 후 다시 보내주세요."

    # "확인!" and "확인." mean "확인". Without this they fall through to a *revision*, which spends
    # a full LLM turn rewriting the note against the "instruction" 확인! — at the exact moment the
    # owner thought they were done.
    lowered = text.lower().rstrip("!.…~ ")
    if lowered in _CANCEL:
        store.remove(review.message_id)
        return (
            "🗑 검토를 취소하고 초안을 버렸습니다.\n"
            "(원본 메모는 Saved Messages에 그대로 있으니 필요하면 다시 보내주세요)"
        )

    if lowered in _ACCEPT:
        # Accepting needs no engine call: the draft on disk *is* what the owner just approved.
        path = _write(review, settings)
        store.remove(review.message_id)
        logger.info("review %s accepted: %s", review.message_id, path.name)
        return f"✅ 저장됨: {path.name}"

    return await _revise(review, text, settings, store=store, engine=engine)


async def _revise(
    review: PendingReview,
    text: str,
    settings: Settings,
    *,
    store: SessionStore,
    engine: ClaudeCLI | None,
) -> str:
    """Resume the session with the owner's correction and show them the revision."""
    review.state = ReviewState.FINALIZING
    store.update(review)
    engine = engine or build_engine(settings)

    try:
        result = await engine.run(
            prompts.render(
                "review_revise",
                text=text,
                sentinel=_SENTINEL,
                questions_heading=QUESTIONS_HEADING,
            ),
            system_prompt=_SYSTEM_PROMPT,
            resume=review.session_id,
            add_dirs=[review.work_dir],
            cwd=review.work_dir,
            timeout_sec=max(settings.claude_timeout_sec, _REVIEW_TIMEOUT_SEC),
        )
        if _draft_failed(result.text):
            raise ClaudeError("수정본을 만들지 못했습니다")
    except ClaudeSessionLost as exc:
        # Terminal: the transcript is gone, so there is no turn 3 to wait for. Hand over the draft.
        logger.warning("session lost for review %s: %s", review.message_id, exc)
        return deliver_draft(review, settings, store, "검토 세션이 사라졌습니다")
    except ClaudeUsageLimit as exc:
        # Survivable: the transcript is on disk and resumes after the window resets. Stay in
        # AWAITING_REVIEW and let the owner re-send. Deliberately does **not** halt the bot —
        # halting stops the poller, which is the only way this reply can arrive.
        # Deliberately not counted as a failure: the engine is not broken, the allowance is spent.
        # Counting it would give up on a perfectly healthy review after three retries inside one
        # limit window. Expiry is the backstop that keeps this from waiting forever.
        logger.warning("usage limit during review %s: %s", review.message_id, exc)
        return _stay(
            review,
            store,
            "⏸ 사용량 한도에 걸려 지금은 반영하지 못했습니다.\n"
            "한도가 리셋된 뒤 답장을 다시 보내주세요 — 초안은 그대로 있습니다.",
        )
    except ClaudeError as exc:
        logger.warning("revision failed for review %s: %s", review.message_id, exc)
        review.failures += 1
        if review.failures >= _MAX_FAILURES:
            return deliver_draft(
                review, settings, store, f"수정을 {_MAX_FAILURES}번 연속 실패했습니다 ({exc})"
            )
        return _stay(review, store, f"⚠️ 반영하지 못했습니다 — {exc}\n다시 보내주시면 재시도합니다.")

    title, body, questions = parse_draft(result.text)
    if title:
        review.title = title
    review.questions = questions
    review.write_draft(body)
    review.failures = 0
    review.state = ReviewState.AWAITING_REVIEW
    store.update(review)
    return review_block(review)


def _stay(review: PendingReview, store: SessionStore, message: str) -> str:
    """Keep the review open and answerable after a recoverable failure."""
    review.state = ReviewState.AWAITING_REVIEW
    store.update(review)
    return message


# ------------------------------------------------------------------------ expiry
async def expire_stale(
    settings: Settings, *, store: SessionStore, now: datetime | None = None
) -> str | None:
    """Deliver the draft of a review the owner never answered. Returns what to say, or None.

    An expiry is an *involuntary* end, so it delivers rather than discards. The bound also keeps
    the review inside the Bot API's 24h update-retention window: past that, a reply the owner sends
    while the app is closed is dropped by Telegram, so the review could never be finished anyway.
    """
    review = store.pending()
    if review is None:
        return None
    age = review.age_hours(now=now or datetime.now(timezone.utc))
    if age < settings.review_expiry_hours:
        return None
    logger.info("review %s expired after %.1fh", review.message_id, age)
    return deliver_draft(
        review, settings, store, f"{settings.review_expiry_hours:.0f}시간 동안 답장이 없었습니다"
    )
