"""Pending-review store: the durable record of a human-in-the-loop review in flight.

A review spans *turns* and therefore outlives the handler that started it. This module is what
carries it across that gap — and across an app restart, which the measured resume contract
(docs/PHASE2.md, "The resume contract") says a Claude session survives.

Three facts from that contract shape everything here:

1. **A session is keyed by its working-directory path** (``~/.claude/projects/<slugified-cwd>/
   <session-id>.jsonl``). So a review's directory must live until the review ends — the
   ``TemporaryDirectory`` pattern increments 2-3 use would delete the session out from under the
   owner before they had read the question. Hence :attr:`PendingReview.work_dir`, a real directory
   under ``state/reviews/``, removed only when the review ends.
2. **The session id can be pinned up front** (``--session-id``), so the resume handle is recorded
   *before* the call that creates it. A crash mid-call leaves something resumable rather than an
   orphan.
3. **Resuming does not fork the id** (measured: the same id came back on turns 2 and 3), so the
   handle is written once and stays valid for the life of the review.

**The JSON file is the authority, not an in-memory dict.** Both the capture handler and the client
service hold their own :class:`SessionStore` over the same path, so a cached copy in either would
drift from the other. Every operation therefore loads, mutates, and atomically writes. The file
holds a handful of entries and is touched at human speed, so the cost is irrelevant and the
cache-coherence bug it removes is not.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger("contextbot.session_store")

_INDEX_NAME = "index.json"


def _aware(value: datetime) -> datetime:
    """Force a timestamp to UTC-aware, so arithmetic can never trip over a naive index entry."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class ReviewState(str, Enum):
    """Where a review is in its lifecycle. ``IDLE`` is represented by absence from the store."""

    # Drafted but **not yet asked about**: waiting its turn behind another review, or still being
    # drafted. The owner has seen nothing, so nothing they say can be an answer to it. Increment 5.
    QUEUED = "queued"
    AWAITING_REVIEW = "awaiting_review"
    # A turn is in flight. Guards against a second reply arriving mid-turn and resuming the same
    # session twice — the CLI would be running two writers over one transcript.
    FINALIZING = "finalizing"


@dataclass
class PendingReview:
    """One review in flight: the draft, the resume handle, and where its session lives."""

    message_id: int
    # Pinned before the first call (--session-id), so this is a valid handle even if that call died.
    session_id: str
    # The job's cwd — what the Claude session is keyed by. Must exist until the review ends.
    work_dir: Path
    draft_path: Path
    title: str
    # When the owner was **asked** — i.e. the earliest moment this review could have been answered.
    # Drives expiry, and tells the poller which bot-DM messages could possibly be answers to it.
    # `create` stamps it provisionally (the record needs a value) and the producer re-stamps it once
    # the draft is ready, because the draft turn sits in between and is minutes long for audio.
    # Distinct from source_date on purpose: a memo captured at catch-up may be hours old, and
    # anything the owner said to the bot in that gap answers something else.
    created_at: datetime
    # The captured message's own date — what the finished note is dated by.
    source_date: datetime
    state: ReviewState = ReviewState.AWAITING_REVIEW
    # Consecutive failed turns. The give-up counter: a review that cannot make progress delivers
    # its draft rather than sitting in AWAITING_REVIEW forever.
    failures: int = 0
    questions: str = ""
    # What the finished note is. Increment 4 hardcoded `note_type="note"` and `tags=[]` in `_write`
    # — the memo's answer smuggled into shared code. A meeting note is `meeting-note` (the vault's
    # own convention: 35 notes carry it, none carry `meeting`), so the *producer* has to own both.
    # The trap is that nothing fails if it does not: the note just lands with the wrong `type:` in
    # its frontmatter, and only a reader who looks will ever notice.
    note_type: str = "note"
    tags: list[str] = field(default_factory=list)
    # The vault's filename slot (`YYMMDD-회의-…`). Travels with the review for the same reason
    # note_type does: it is the *producer's* claim about what this note is, and the shared writer
    # has no way to know. Distinct from note_type on purpose — `meeting-note` is the frontmatter
    # `type:`, `회의` is the filename, and the vault uses both, differently.
    category: str = "노트"

    @property
    def review_dir(self) -> Path:
        """The review's own tree. Everything under it dies when the review ends."""
        return self.work_dir.parent

    @property
    def audio_dir(self) -> Path:
        """Where a media original waits out the review.

        Inside the review's tree but **outside `work_dir`**, beside the draft, for two reasons that
        both matter. It must die with the review — and it does, because `remove()` drops the whole
        tree, which is what makes "delete the audio on success" a thing that *happens* rather than a
        thing someone has to remember. And it must stay out of the model's sight: `work_dir` is the
        cwd and the sole `--add-dir`, so an `.m4a` sitting in it is a file `Read` would hand back as
        raw bytes — the increment-3 `.heic` trap, where the model describes the header and reports
        success. The model has the transcript; it has no use for the audio it cannot hear.
        """
        return self.review_dir / "audio"

    @property
    def draft(self) -> str:
        """The draft body as last written, or "" if it somehow went missing."""
        try:
            return self.draft_path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("draft missing for review %s at %s", self.message_id, self.draft_path)
            return ""

    def write_draft(self, body: str) -> None:
        self.draft_path.parent.mkdir(parents=True, exist_ok=True)
        self.draft_path.write_text(body, encoding="utf-8")

    def age_hours(self, *, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (now - self.created_at).total_seconds() / 3600.0

    def to_json(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "session_id": self.session_id,
            "work_dir": str(self.work_dir),
            "draft_path": str(self.draft_path),
            "title": self.title,
            "created_at": self.created_at.isoformat(),
            "source_date": self.source_date.isoformat(),
            "state": self.state.value,
            "failures": self.failures,
            "questions": self.questions,
            "note_type": self.note_type,
            "tags": list(self.tags),
            "category": self.category,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "PendingReview":
        return cls(
            message_id=int(data["message_id"]),
            session_id=str(data["session_id"]),
            work_dir=Path(data["work_dir"]),
            draft_path=Path(data["draft_path"]),
            title=str(data.get("title") or ""),
            created_at=_aware(datetime.fromisoformat(data["created_at"])),
            source_date=_aware(datetime.fromisoformat(data["source_date"])),
            state=ReviewState(data.get("state") or ReviewState.AWAITING_REVIEW.value),
            failures=int(data.get("failures") or 0),
            questions=str(data.get("questions") or ""),
            # Every optional field defaults, so widening the persisted schema costs nothing: a
            # review written by the previous version still loads.
            note_type=str(data.get("note_type") or "note"),
            tags=list(data.get("tags") or []),
            category=str(data.get("category") or "노트"),
        )


class SessionStore:
    """The reviews and the bot-DM update offset, persisted under ``state/reviews/``.

    **At most one review is ever *asked about*** (:meth:`pending`), and that constraint is
    unchanged from increment 4: with two open, a bare "확인" in the bot DM cannot be attributed to
    one of them, and guessing would silently apply the owner's answer to the wrong draft.

    **Increment 5 adds a queue rather than relaxing it.** A memo bouncing off "one at a time" cost a
    re-send; an audio file bouncing off it would cost a re-upload *and* a whole Whisper run. So a
    review that arrives while another is being asked about is drafted anyway and parked in
    :attr:`ReviewState.QUEUED` — the expensive work is already done and kept, and only the *asking*
    waits. Promotion is then a timestamp and a DM: it makes no engine call and cannot fail.

    So the store holds many reviews but at most one answerable one, which is the distinction every
    method here turns on.
    """

    def __init__(self, root: Path):
        self._root = root
        self._index = root / _INDEX_NAME

    @property
    def root(self) -> Path:
        return self._root

    # ------------------------------------------------------------------ load/save
    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._index.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return {"reviews": [], "update_offset": 0}
        if not isinstance(data, dict):
            return {"reviews": [], "update_offset": 0}
        data.setdefault("reviews", [])
        data.setdefault("update_offset", 0)
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._root), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._index)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # --------------------------------------------------------------------- access
    def all_reviews(self) -> list[PendingReview]:
        """Every review on disk, oldest first. Malformed entries are dropped rather than raising."""
        reviews: list[PendingReview] = []
        for entry in self._load()["reviews"]:
            try:
                reviews.append(PendingReview.from_json(entry))
            except (KeyError, ValueError, TypeError):
                logger.warning("dropping unreadable review entry: %r", entry)
        return reviews

    def pending(self) -> PendingReview | None:
        """The review the owner has been **asked** about, or None.

        A ``QUEUED`` review is deliberately invisible here, and that is what makes the queue safe:
        the owner has not seen it, so nothing they type can be an answer to it. It is also what
        keeps a review that is still *being drafted* from taking a reply meant for something else —
        turn 1 creates the entry queued and only activates it once there is a draft to show.
        """
        for review in self.all_reviews():
            if review.state is not ReviewState.QUEUED:
                return review
        return None

    def has_pending(self) -> bool:
        return self.pending() is not None

    def queued(self) -> list[PendingReview]:
        """Reviews drafted and waiting their turn to be asked about, oldest first."""
        return [r for r in self.all_reviews() if r.state is ReviewState.QUEUED]

    def get(self, message_id: int) -> PendingReview | None:
        for review in self.all_reviews():
            if review.message_id == message_id:
                return review
        return None

    def create(
        self,
        *,
        message_id: int,
        session_id: str,
        title: str,
        source_date: datetime,
        created_at: datetime | None = None,
        note_type: str = "note",
        category: str = "노트",
    ) -> PendingReview:
        """Reserve a review: make its stable directories and record the resume handle.

        Called **before** the first engine call, so the pinned ``session_id`` is already on disk if
        that call dies partway. ``work_dir`` is the session's cwd and is deliberately left *empty*
        — the isolation rule from increments 2-3 still binds, so a job that reads a file stages it
        there alone. The draft lives one level up, outside the cwd, where the model cannot see it.

        It is created **QUEUED**, always, and the producer activates it once a draft exists. That
        ordering is not bookkeeping: a review created answerable would be visible to :meth:`pending`
        for the whole drafting turn — minutes, for audio — so a reply that arrived meanwhile would
        be routed into a review with no draft, answering a question nobody had been asked.
        """
        base = self._root / str(message_id)
        work_dir = base / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        review = PendingReview(
            message_id=message_id,
            session_id=session_id,
            work_dir=work_dir,
            draft_path=base / "draft.md",
            title=title,
            created_at=_aware(created_at or datetime.now(timezone.utc)),
            source_date=source_date,
            state=ReviewState.QUEUED,
            note_type=note_type,
            category=category,
        )
        self._put(review)
        logger.info("review %s created (session=%s)", message_id, session_id)
        return review

    def update(self, review: PendingReview) -> None:
        """Persist a mutated review (state, failure count, title, tags)."""
        self._put(review)

    def _put(self, review: PendingReview) -> None:
        """Insert or replace one review, leaving every other entry alone.

        Increment 4 wrote ``data["reviews"] = [review]`` here, which was correct while exactly one
        review could exist and is a silent eraser now: saving the active review would drop the whole
        queue on the floor, and nothing would raise.
        """
        data = self._load()
        entries = [e for e in data["reviews"] if str(e.get("message_id")) != str(review.message_id)]
        entries.append(review.to_json())
        data["reviews"] = entries
        self._save(data)

    def remove(self, message_id: int) -> None:
        """End a review: drop it from the index and delete its directory tree.

        The session transcript in ``~/.claude/projects/`` is Claude Code's own to keep; removing
        the work dir only makes it unresumable, which is correct — the review is over.
        """
        data = self._load()
        data["reviews"] = [
            e for e in data["reviews"] if str(e.get("message_id")) != str(message_id)
        ]
        self._save(data)
        shutil.rmtree(self._root / str(message_id), ignore_errors=True)
        logger.info("review %s ended", message_id)

    # ------------------------------------------------------------- update offset
    @property
    def update_offset(self) -> int:
        """The next ``get_updates`` offset, persisted so a restart cannot replay an old reply."""
        return int(self._load().get("update_offset") or 0)

    def set_update_offset(self, offset: int) -> None:
        data = self._load()
        data["update_offset"] = int(offset)
        self._save(data)


def build_store(settings) -> SessionStore:
    """Construct a :class:`SessionStore` from :class:`~contextbot.config.Settings`.

    Mirrors ``build_engine``: the store lives beside the HWM under ``state/``, so both survive a
    restart the same way and neither needs a setting of its own.
    """
    return SessionStore(settings.session_path.parent / "reviews")
