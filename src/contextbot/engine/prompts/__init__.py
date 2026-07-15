"""Prompt templates for the `claude -p` engine.

Templates live next to this module as ``*.md`` files so prompts can be edited and reviewed as
prose rather than buried in Python string literals. They use :meth:`str.format` placeholders, so
literal braces in a template must be doubled (``{{`` / ``}}``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent


class PromptNotFound(KeyError):
    """Raised when a template name has no corresponding ``.md`` file."""


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """Return the raw text of the ``<name>.md`` template."""
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise PromptNotFound(f"no prompt template named {name!r} in {_PROMPTS_DIR}")
    return path.read_text(encoding="utf-8")


def render(name: str, **values: object) -> str:
    """Load ``<name>.md`` and substitute ``{placeholder}`` values into it."""
    return load(name).format(**values)
