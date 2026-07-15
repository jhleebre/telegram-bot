"""Filename generation for notes: ``YYMMDD-HHMM-<slug>.md`` with collision handling."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

_MAX_SLUG_LEN = 40
# Keep word characters (incl. Unicode letters like Hangul) and spaces; drop the rest.
_STRIP_RE = re.compile(r"[^\w\s-]", flags=re.UNICODE)
_SPACE_RE = re.compile(r"[\s_]+", flags=re.UNICODE)


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


def build_filename(text: str, when: datetime, *, extension: str = "md") -> str:
    """Return ``YYMMDD-HHMM-<slug>.<extension>`` (no collision suffix)."""
    prefix = when.strftime("%y%m%d-%H%M")
    return f"{prefix}-{slugify(text)}.{extension}"


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
