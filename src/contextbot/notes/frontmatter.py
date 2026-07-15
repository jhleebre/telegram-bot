"""Build YAML frontmatter for generated Markdown notes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import yaml


def build_frontmatter(
    *,
    title: str,
    date: datetime,
    source: str = "telegram",
    note_type: str = "note",
    tags: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the frontmatter mapping (ordering preserved for rendering)."""
    data: dict[str, Any] = {
        "title": title,
        "date": date.isoformat(),
        "source": source,
        "type": note_type,
        "tags": tags if tags is not None else [],
    }
    if extra:
        data.update(extra)
    return data


def render_frontmatter(data: dict[str, Any]) -> str:
    """Render a frontmatter mapping into a ``---`` delimited YAML block."""
    body = yaml.safe_dump(
        data,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    return f"---\n{body}---\n"


def render_note(frontmatter: dict[str, Any], body: str) -> str:
    """Combine frontmatter and a Markdown body into a full note document."""
    block = render_frontmatter(frontmatter)
    body = body.rstrip("\n")
    return f"{block}\n{body}\n"
