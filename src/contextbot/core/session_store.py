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
from dataclasses import dataclass
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
    # When the *review* began. Drives expiry, and tells the poller which bot-DM messages could
    # possibly be answers to it. Distinct from source_date on purpose: a memo captured at catch-up
    # may be hours old, and anything the owner said to the bot in that gap answers something else.
    created_at: datetime
    # The captured message's own date — what the finished note is dated by.
    source_date: datetime
    state: ReviewState = ReviewState.AWAITING_REVIEW
    # Consecutive failed turns. The give-up counter: a review that cannot make progress delivers
    # its draft rather than sitting in AWAITING_REVIEW forever.
    failures: int = 0
    questions: str = ""

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
        )


class SessionStore:
    """The pending reviews and the bot-DM update offset, persisted under ``state/reviews/``.

    **At most one review is active at a time** (:meth:`pending`). That is a deliberate constraint,
    not a missing feature: with two reviews open, a bare "확인" in the bot DM cannot be attributed
    to one of them, and guessing would silently apply the owner's answer to the wrong draft. See
    docs/PHASE2.md — increment 5 is where a queue gets designed, with real audio ergonomics to
    design it against.
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
    def pending(self) -> PendingReview | None:
        """The active review, or None. Malformed entries are dropped rather than raising."""
        for entry in self._load()["reviews"]:
            try:
                return PendingReview.from_json(entry)
            except (KeyError, ValueError, TypeError):
                logger.warning("dropping unreadable review entry: %r", entry)
        return None

    def has_pending(self) -> bool:
        return self.pending() is not None

    def create(
        self,
        *,
        message_id: int,
        session_id: str,
        title: str,
        source_date: datetime,
        created_at: datetime | None = None,
    ) -> PendingReview:
        """Reserve a review: make its stable directories and record the resume handle.

        Called **before** the first engine call, so the pinned ``session_id`` is already on disk if
        that call dies partway. ``work_dir`` is the session's cwd and is deliberately left *empty*
        — the isolation rule from increments 2-3 still binds, so a job that reads a file stages it
        there alone. The draft lives one level up, outside the cwd, where the model cannot see it.
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
        )
        data = self._load()
        data["reviews"] = [review.to_json()]
        self._save(data)
        logger.info("review %s created (session=%s)", message_id, session_id)
        return review

    def update(self, review: PendingReview) -> None:
        """Persist a mutated review (state, failure count, title)."""
        data = self._load()
        data["reviews"] = [review.to_json()]
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
