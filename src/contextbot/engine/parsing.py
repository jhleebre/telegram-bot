"""Helpers for reading structured data out of a model's free-text reply.

Models asked for "JSON and nothing else" still occasionally wrap the object in ``` fences or add a
sentence of preamble. These helpers tolerate that instead of failing the whole pipeline.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


class ParseError(ValueError):
    """Raised when no JSON object can be recovered from the text."""


def _candidates(text: str):
    """Yield progressively looser candidate substrings that might be the JSON object."""
    stripped = text.strip()
    yield stripped

    fenced = _FENCE_RE.search(stripped)
    if fenced:
        yield fenced.group(1)

    # Fall back to the outermost {...} span, covering leading/trailing prose.
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        yield stripped[start : end + 1]


def extract_json_object(text: str) -> dict[str, Any]:
    """Return the JSON object found in ``text``.

    Raises :class:`ParseError` when nothing parses as a JSON object.
    """
    for candidate in _candidates(text):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise ParseError(f"no JSON object in model output: {text[:200]!r}")
