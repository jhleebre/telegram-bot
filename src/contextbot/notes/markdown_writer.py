"""Atomic writing of Markdown notes into the inbox directory."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .frontmatter import build_frontmatter, render_note
from .naming import build_filename, unique_path


def write_note(
    *,
    inbox_dir: Path,
    body: str,
    title: str,
    when: datetime,
    source: str = "telegram",
    note_type: str = "note",
    tags: list[str] | None = None,
    extra: dict[str, Any] | None = None,
    slug_source: str | None = None,
) -> Path:
    """Render and atomically write a note; return the final path.

    ``slug_source`` controls the filename slug (defaults to ``title``). The write is atomic:
    content goes to a temp file in the same directory, then ``os.replace`` moves it into place.
    """
    inbox_dir.mkdir(parents=True, exist_ok=True)

    frontmatter = build_frontmatter(
        title=title,
        date=when,
        source=source,
        note_type=note_type,
        tags=tags,
        extra=extra,
    )
    content = render_note(frontmatter, body)

    filename = build_filename(slug_source if slug_source is not None else title, when)
    target = unique_path(inbox_dir, filename)

    fd, tmp_name = tempfile.mkstemp(dir=str(inbox_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, target)
    except BaseException:
        # Clean up the temp file on any failure so we never leave `.tmp` litter behind.
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    return target
