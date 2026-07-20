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


# Caption text is *substituted into* this template, never formatted as one, so braces the owner
# happened to type are inert. Same reason every other value is safe: `render` formats the template,
# not the values.
def caption_section(caption: str | None) -> str:
    """Render the shared caption block, or ``""`` when the owner typed nothing.

    Every media route asks for this and drops the result into its own ``{caption}`` slot, so the
    policy for what a caption may and may not do is written once (``caption.md``) rather than
    drifting between four prompts. An empty string is the whole no-caption path: the templates read
    identically to how they did before captions existed, so a bare file behaves exactly as it did.
    """
    if not caption or not caption.strip():
        return ""
    return render("caption", caption=caption.strip())
