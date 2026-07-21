"""The human-in-the-loop review loop: draft → ask → revise → accept.

Built in increment 4, and this module owns a review's whole lifecycle from the moment a draft
exists — the owner's bot-DM replies, revisions, acceptance, and every way it can end. A *producer*
(today: `handlers/audio_handler.py`) makes the draft and hands it over; nothing about the machinery
below is written twice.

It was built and verified on a memo prefixed `#검토`, deliberately — the delivery approach says to
stand the state machine up on something cheap before the audio pipeline depends on it, and a memo
gave the same multi-turn resume contract with no Whisper in the path. **Increment 5 deleted that
scaffolding once audio became the real producer**: a magic prefix in the *capture* channel made
"throw it in Saved Messages and it becomes a note" conditional, which is the design that produced
the `#검토된 사항` mangling bug. Its removal is why nothing here knows what a memo is.

The rules the increment's open decision settled (recorded in full in docs/PHASE2.md):

- **A review turn never raises `DeferMessage`.** There is no handler on the stack to replay, replay
  would re-run the whole job over a draft that already exists, and halting stops the poller — i.e.
  the very channel the reply must arrive on. `DeferMessage` belongs to capture (turn 1) only.
- **A review never ends empty-handed.** A lost session, a review that cannot make progress, and an
  expired one all end by *delivering the draft* as a note marked unreviewed. Only `취소` discards,
  because the intent is unambiguous and Saved Messages still holds the input.
- **A usage limit mid-review keeps the review alive.** The transcript is on disk and resumes after
  the window resets, so the owner just re-sends their reply. Nothing is written, nothing is lost.
- **At most one review is ever *asked about*** — increment 5 added a queue rather than relaxing
  that, because a bare `확인` must always be attributable. See `core/session_store.py`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings
from ..core.session_store import PendingReview, ReviewState, SessionStore
from ..engine import prompts
from ..engine.claude_cli import (
    ClaudeCLI,
    ClaudeError,
    ClaudeSessionLost,
    ClaudeUsageLimit,
    build_engine,
)
from ..files.glossary import GlossaryEntry, append_entries, parse_entries
from ..notes.markdown_writer import write_note
from .text_handler import clean_title

logger = logging.getLogger("contextbot.handlers.conversation")

QUESTIONS_HEADING = "## 확인 요청"
_TITLE_PREFIX = "제목:"
_TAGS_PREFIX = "태그:"
# How far into a turn's output to look for the 제목: line before giving up (see strip_preamble).
# A preamble is an opening remark — a few lines at most. Bounding it is what stops a `제목:` deep
# inside a real note's body from being mistaken for the title and discarding everything above it.
_MAX_PREAMBLE_LINES = 8
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

# The vault's own convention, read off the vault rather than chosen: 35 notes carry
# `type: meeting-note` and none carry `type: meeting`. Only these reviews have a transcript behind
# them, so only these have anything to teach the glossary.
_MEETING_TYPE = "meeting-note"

_SYSTEM_PROMPT = (
    "You are a note editor for a personal knowledge base. You organise what the author gives you "
    "and ask about what you had to guess. You never invent content you were not given."
)


# --------------------------------------------------------------------------- parsing
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
    stripped = strip_preamble(text).strip()
    title: str | None = None
    first, _, rest = stripped.partition("\n")
    if first.strip().startswith(_TITLE_PREFIX):
        title = clean_title(first.strip()[len(_TITLE_PREFIX) :])
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


# One rule, used twice: below the draft and below the questions. The two together frame the
# questions as the single block the owner acts on, so it never reads as a continuation of the draft.
_RULE = "──────────"
_FOOTER = (
    f"{_RULE}\n"
    "이대로 저장하려면 `확인`, 버리려면 `취소`.\n"
    "고칠 부분이 있으면 그냥 알려주세요 — 반영해서 다시 보여드립니다."
)


def review_block(review: PendingReview) -> str:
    """The bot-DM message that asks the owner to review a draft. Always deliverable.

    The questions are what the owner must actually answer, so they are kept whole and the draft
    gives up whatever room they need — the draft is the part they can read in the note.
    """
    header = f"📝 초안이 준비됐습니다 — 「{review.title}」"
    # A rule above the questions mirrors the one above the footer: the draft ends (often on the
    # elision notice), the rule breaks the flow, and the questions read as their own block.
    questions = f"{_RULE}\n\n{review.questions}" if _has_questions(review.questions) else ""

    # Everything except the draft is fixed cost; the draft takes what is left of the budget.
    fixed = len("\n\n".join(part for part in [header, "", questions, _FOOTER] if part))
    draft = _elide(review.draft, max(_MAX_DM_CHARS - fixed, 0))

    block = "\n\n".join(part for part in [header, draft, questions, _FOOTER] if part)
    # Last resort: even the fixed parts can overrun if the model ignored "two to four questions".
    # A truncated block the owner can answer beats a perfect one Telegram refuses to deliver.
    return _elide(block, _TELEGRAM_MAX_CHARS - len(_ELIDED))


# ------------------------------------------------------------------- writing notes
def strip_preamble(text: str) -> str:
    """Drop anything a turn wrote *before* its `제목:` line. Returns the text unchanged if there
    is nothing to drop.

    **Measured, not defensive.** Both the draft and revise prompts say "output Markdown and nothing
    else — no preamble", and it mostly works: the first real meeting-note run obeyed. The second
    opened with `글로시리 확인이 완료됐습니다. …` and a `---`, which cost everything downstream at
    once — `parse_draft` looks for `제목:` on the **first line**, so the title fell back to the
    filename ("meeting"), the tags came out empty, and the preamble *and the raw `제목:`/`태그:`
    lines* were all saved as the note's body. Nothing raised.

    So the instruction stays (it is right, and it usually works) and the parsing stops depending on
    it — the same shape as increment 2's answer to a flaky PDF conversion: harden the prompt *and*
    make the failure structurally impossible to save.

    Bounded on purpose. Only the first few lines are searched, so a `제목:` occurring naturally deep
    in a note's body can never eat the note above it: a preamble is an opening remark, not content.
    """
    lines = text.strip().splitlines()
    if not lines or lines[0].strip().startswith(_TITLE_PREFIX):
        return text
    for index, line in enumerate(lines[:_MAX_PREAMBLE_LINES]):
        if line.strip().startswith(_TITLE_PREFIX):
            logger.warning("dropping a %d-line preamble before the 제목: line", index)
            return "\n".join(lines[index:])
    return text


def strip_trailing_rule(body: str) -> str:
    """Drop a horizontal rule left at the end of a draft body.

    The prompts ask for the questions heading straight after the note; a model that puts a `---`
    separator before it leaves that rule behind in the body, because `parse_draft` splits on the
    heading and everything above it is the note. Observed on the first real meeting-note run.
    Harmless but visible: the saved note ends in a dangling `<hr>`.
    """
    stripped = body.rstrip()
    while True:
        head, sep, last = stripped.rpartition("\n")
        if not sep or set(last.strip()) not in ({"-"}, {"*"}, {"_"}) or len(last.strip()) < 3:
            return stripped
        stripped = head.rstrip()


def split_tags(body: str) -> tuple[list[str], str]:
    """Pull a `태그:` line off the front of a draft body; return (tags, body).

    Applied on **every** turn, not just the producer's, and that is the point. Turn 1's prompt asks
    for the line; `review_revise.md` says "output the note in the same structure as before", so a
    revision reproduces it — and without this the line would land in the note body as literal text
    (`태그: 에이닷, B2B` as the note's first line). That is increment 4's body-corruption bug exactly:
    nothing raises, the suite stays green, and the note is quietly wrong.

    An absent line is normal, not a failure — it just means no tags this turn.
    """
    stripped = body.lstrip()
    first, _, rest = stripped.partition("\n")
    if not first.strip().startswith(_TAGS_PREFIX):
        return [], body
    raw = first.strip()[len(_TAGS_PREFIX) :]
    tags = [t.strip().lstrip("#") for t in raw.split(",")]
    return [t for t in tags if t], rest.strip()


def _write(review: PendingReview, settings: Settings, *, unreviewed: str | None = None) -> Path:
    """Write the draft as a note. ``unreviewed`` marks one the owner never accepted.

    ``note_type``, ``tags`` and ``category`` come off the *review*, not from here. They were hardcoded to
    ``"note"`` / ``[]`` while `#검토` was the only producer, which was the memo's answer smuggled
    into shared code — a meeting note would have landed as `type: note`, and nothing would have
    failed to say so.
    """
    body = review.draft
    if unreviewed:
        body = (
            f"> ⚠️ 검토를 마치지 못한 초안입니다 — {unreviewed}\n"
            f"> 내용을 그대로 확인해주세요.\n\n{body}"
        )
    extra: dict[str, object] = {
        "telegram_message_id": review.message_id,
        "reviewed": unreviewed is None,
    }
    # The draft already acted on it; this keeps the owner's own words, which acting on them spent.
    if review.caption:
        extra["caption"] = review.caption
    return write_note(
        inbox_dir=settings.inbox_dir,
        body=body,
        title=review.title,
        when=review.source_date,
        source="telegram",
        category=review.category,
        note_type=review.note_type,
        tags=review.tags,
        extra=extra,
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

    Both rules apply to a **queued** review too, not only the answerable one: a crash during a
    queued job's draft turn leaves exactly the same draftless entry, and keeping it would hold a
    place in the queue for a note that does not exist.
    """
    dropped = False
    for review in store.all_reviews():
        if not review.draft_path.exists():
            logger.info("discarding review %s: its first turn never finished", review.message_id)
            store.remove(review.message_id)
            dropped = True
            continue

        if review.state is ReviewState.FINALIZING:
            logger.info("review %s: clearing a turn that died with the app", review.message_id)
            review.state = ReviewState.AWAITING_REVIEW
            store.update(review)
    return dropped


# ---------------------------------------------------------------------- the queue
def activate(review: PendingReview, store: SessionStore) -> str:
    """Ask the owner about a drafted review, and start its clock. Returns the DM to send.

    The clock starts **here**, when the owner is actually asked — not when the work began. For audio
    that gap is minutes of Whisper plus a drafting turn, and for a queued review it can be however
    long the review ahead of it took. ``created_at`` gates the poller's backlog filter, so anything
    typed at the bot before this moment predates the question and cannot be an answer to it; stamped
    any earlier, an unrelated message would sail through and be applied as a correction.
    """
    review.state = ReviewState.AWAITING_REVIEW
    review.created_at = datetime.now(timezone.utc)
    store.update(review)
    return review_block(review)


def promote_next(store: SessionStore) -> str | None:
    """Ask about the oldest queued review, if nothing else is being asked about. Returns the DM.

    **This cannot fail, and that is the whole reason the queue is shaped this way.** Everything
    expensive — the download, Whisper, the drafting turn — already happened in the capture handler,
    where ``DeferMessage`` is legal and there is a handler on the stack to replay. Promotion is a
    timestamp and a message. The alternative (draft it *when its turn comes*) would run an engine
    call from a background task with no handler to replay, and would need its own retry driver, its
    own give-up counter, and its own answer for a usage limit — a second state machine beside the
    one this module already is.
    """
    if store.has_pending():
        return None
    # Only a review that **has its draft**. A producer creates its entry QUEUED and then spends
    # minutes drafting, so the queue also contains work still in flight — and promoting that races
    # the producer for the same entry. Both halves lose: the owner gets "초안이 준비됐습니다" with an
    # empty body (the draft file does not exist yet), and the producer then writes its own object
    # back over the activation, leaving a finished draft QUEUED with nothing pending — so the poller
    # stops and nobody is asked until the next restart. The draft file is the same signal
    # `discard_incomplete` trusts: only a completed turn creates it.
    queue = [review for review in store.queued() if review.draft_path.exists()]
    if not queue:
        return None
    review = queue[0]
    waiting = len(queue) - 1
    logger.info("promoting queued review %s (%d still waiting)", review.message_id, waiting)
    header = "🎙 다음 회의록 차례입니다."
    if waiting:
        header += f" (뒤에 {waiting}건 더 대기 중)"
    return f"{header}\n\n{activate(review, store)}"


# ------------------------------------------------- talking to the bot otherwise
# Plain text, deliberately. `Notifier.send` calls `send_message(chat_id, text)` with **no
# parse_mode**, so Telegram renders every reply literally — `**bold**` would arrive as asterisks.
# And turning parse_mode on would be worse than untidy: note filenames are full of underscores
# (`260716-회의-하반기_모두의_ai_전략_덱_검토.md`), which Markdown reads as italics, and a parse
# error makes Telegram reject the message — which `Notifier` *swallows*, so a confirmation would
# vanish rather than fail. Backticks are the project's existing convention for "type this", and
# they read fine as quotes even unrendered.
_USAGE = "메모·파일·녹음은 Saved Messages로 보내주세요."


def bot_dm_status(store: SessionStore, *, stale: bool = False, usage: str | None = None) -> str:
    """What to say to an owner's message that no review is going to act on.

    **Every message gets an answer, and the rule has no exceptions** — that is the point of it.
    Telegram has no offline autoresponder to borrow (a bot is a token plus your code; when the code
    is down, nothing answers), so the closest honest substitute is to answer *without fail* while
    the app is up. Then **silence means exactly one thing: nothing is running.** Coalescing replies,
    or staying quiet when there is "nothing to say", would hand that meaning back to ambiguity — the
    silent no-op this replaces, where the owner could not tell a stopped bot from a broken one.

    ``stale`` is the sharp case: the message was sent before the open review was asked about, so it
    cannot be that review's answer (the poller's backlog rule). Saying so matters — the owner very
    likely typed `확인` at a question they had not yet been shown, and a bare status reply would
    look like their answer had been ignored.

    ``usage`` is the plan's remaining allowance (see `engine/usage.py`), and it rides on the *idle*
    reply only. That is the message this proves-it-is-alive rule generates most of, and the one with
    nothing else in it — while a review is open the reply's job is to name the draft that is waiting,
    and a limits table under it would bury the question the owner still has to answer. Passed in
    rather than read here so this stays pure: the reading is a subprocess call.
    """
    review = store.pending()
    if review is None:
        parts = ["🤖 실행 중입니다 — 지금은 검토 중인 초안이 없습니다.", _USAGE, usage or ""]
        return "\n\n".join(part for part in parts if part)

    asking = (
        f"📝 검토 중인 초안이 있습니다 — 「{review.title}」\n"
        "고칠 부분을 알려주시거나, `확인` / `취소` 로 답해주세요."
    )
    if not stale:
        return asking
    return (
        "⏳ 방금 그 메시지는 이 초안이 준비되기 전에 보내신 거라, 답장으로 반영하지 않았습니다.\n"
        "(초안을 보시기 전에 하신 말이라 이 초안에 대한 답일 수는 없어서요)\n\n"
        f"{asking}"
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
        return await _accept(review, settings, store=store, engine=engine)

    return await _revise(review, text, settings, store=store, engine=engine)


async def _accept(
    review: PendingReview,
    settings: Settings,
    *,
    store: SessionStore,
    engine: ClaudeCLI | None,
) -> str:
    """The owner said yes: write the note, file what the review taught us, end the review.

    Increment 4's accept was one line — the draft on disk *is* what they approved, so no engine call
    was needed. Increment 5 makes "success" mean two more things, and both land here:

    - **the glossary** learns the mis-transcriptions this review confirmed, and
    - **the audio is deleted**, which happens for free: it lives in the review's tree, and
      ``store.remove`` drops that tree.

    The order is the whole safety argument. The glossary turn is an engine call, so it goes
    **first**, and every side effect follows it — the same rule the capture handlers hold, applying
    one last time at the end of a review rather than the start of a job.

    **Nothing here may cost the owner their note.** They said 확인; the note is the deliverable, and
    a glossary that could not be updated is a footnote. So the note is written whatever the glossary
    turn does, and the failure is reported rather than raised.
    """
    terms: list[GlossaryEntry] = []
    glossary_note = ""
    if review.note_type == _MEETING_TYPE and settings.claude_enabled:
        try:
            terms = await _confirmed_terms(review, settings, engine=engine)
        except ClaudeError as exc:
            # Every reason to be here is survivable: a usage limit, a lost session, a timeout. None
            # of them is a reason to withhold the note the owner just approved.
            logger.warning("glossary turn failed for review %s: %s", review.message_id, exc)
            glossary_note = f"\n⚠️ 용어집은 갱신하지 못했습니다 — {exc}"

    # Everything below is a side effect, so nothing above it may be one.
    path = _write(review, settings)
    if terms:
        added = append_entries(settings.glossary_path, terms)
        if added:
            glossary_note = "\n📖 용어집에 추가: " + ", ".join(
                f"{e.transcribed} → {e.correct}" for e in added
            )
    store.remove(review.message_id)  # takes the review's tree, and the audio in it, with it
    logger.info("review %s accepted: %s", review.message_id, path.name)
    return f"✅ 저장됨: {path.name}{glossary_note}"


async def _confirmed_terms(
    review: PendingReview, settings: Settings, *, engine: ClaudeCLI | None
) -> list[GlossaryEntry]:
    """Resume the session once and ask what this review settled about the recogniser.

    Only the conversation knows. The model proposed the corrections, the owner accepted some and
    overrode others, and the difference is not derivable from the note — which is why turn 1's
    *proposals* are not stored and appended instead: a proposal the owner corrected would then be
    filed as though confirmed, silently mis-correcting every future meeting note. A wrong glossary
    entry is worse than no glossary at all.
    """
    engine = engine or build_engine(settings)
    result = await engine.run(
        prompts.render("meeting_glossary"),
        system_prompt=_SYSTEM_PROMPT,
        resume=review.session_id,
        add_dirs=[review.work_dir],
        cwd=review.work_dir,
        timeout_sec=max(settings.claude_timeout_sec, _REVIEW_TIMEOUT_SEC),
    )
    return parse_entries(result.text)


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
    # `review_revise.md` asks for the note "in the same structure as before", so a revision of a
    # meeting note reproduces the 태그: line. Strip it here or it becomes the note body's first line.
    tags, body = split_tags(body)
    if tags:
        review.tags = tags
    body = strip_trailing_rule(body)
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
