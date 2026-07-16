"""Filename generation for notes: ``YYMMDD-<분류>-<slug>.md`` with collision handling.

**The middle segment is the vault's own convention, read off the vault rather than invented.** Of
the 181 notes there, 103 are named ``YYMMDD-<두 글자>-<topic>``: 회의 (41), 전략 (40), 조사 (12),
보고 (3), 안건 (2), and one each of 초안/의견/배경/기획. The bot used to write ``YYMMDD-HHMM-<slug>``
and **not one such file exists in the vault** — so there was never anything to stay consistent with,
only a convention to join.

The category is a *filename* slot, not a frontmatter tag: the vault's own meeting notes carry tags
like `에이닷`/`B2B` and never `회의`. Don't put it in both.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path

_MAX_SLUG_LEN = 40
# Keep word characters (incl. Unicode letters like Hangul) and spaces; drop the rest.
_STRIP_RE = re.compile(r"[^\w\s-]", flags=re.UNICODE)
_SPACE_RE = re.compile(r"[\s_]+", flags=re.UNICODE)

# A recording is a meeting; anything the owner wrote or shot is a note. Both are fixed at the
# handler, because no classifier is needed to know which one you are looking at.
MEETING_CATEGORY = "회의"
NOTE_CATEGORY = "노트"

# What a *document* may be classified as — the owner's list, and every one of them is already in
# use in the vault. Closed on purpose: the category lands in a filename, so an open set would let a
# model's improvisation ("전략적 분석", or a `/`) name a file. Anything outside it becomes
# NOTE_CATEGORY, which is the honest answer for "we could not tell".
DOCUMENT_CATEGORIES = ("전략", "기획", "조사", "안건", "보고", "초안")


def normalize_category(value: str | None) -> str:
    """Coerce a model-supplied document category to one we will actually put in a filename.

    **NFC-normalized first, and that is not paranoia**: the vault already contains one `전략` written
    as decomposed jamo (NFD) alongside 40 composed ones, so "the model returned 전략" and "the string
    equals 전략" are not the same question. Without this, an NFD answer silently falls back to 노트
    and nobody could see why by reading it.
    """
    text = unicodedata.normalize("NFC", (value or "").strip())
    return text if text in DOCUMENT_CATEGORIES else NOTE_CATEGORY


def slugify(text: str, *, max_len: int = _MAX_SLUG_LEN) -> str:
    """Turn arbitrary text into a filesystem-safe slug.

    Lowercases ASCII, keeps Unicode letters (Korean preserved), collapses whitespace and
    underscores to a single ``_``, and truncates. Returns ``"note"`` when nothing usable remains.
    """
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    cleaned = _STRIP_RE.sub(" ", first_line)
    cleaned = _SPACE_RE.sub("_", cleaned.strip())
    cleaned = cleaned.strip("_").lower()
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip("_")
    return cleaned or "note"


def build_filename(text: str, when: datetime, *, category: str, extension: str = "md") -> str:
    """Return ``YYMMDD-<category>-<slug>.<extension>`` (no collision suffix).

    ``category`` has **no default**, deliberately: it is a claim about what the note *is*, and every
    route knows the answer. A default here would let a new route silently inherit someone else's.

    The clock is gone from the name (it was ``YYMMDD-HHMM-``), so two notes on one day can now
    collide — :func:`unique_path` is what makes that safe, and it already did the job for the rare
    same-minute collision.
    """
    return f"{when.strftime('%y%m%d')}-{category}-{slugify(text)}.{extension}"


def unique_path(directory: Path, filename: str) -> Path:
    """Return a non-colliding path inside ``directory`` for ``filename``.

    If ``name.md`` exists, tries ``name-2.md``, ``name-3.md``, …
    """
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        candidate = directory / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1
