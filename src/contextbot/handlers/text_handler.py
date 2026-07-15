"""Text handler: a text memo becomes a Markdown note in the inbox.

Phase 2 adds an optional LLM enrichment pass (`claude -p` → title / tags / summary). It is
strictly an *upgrade*: any failure — CLI missing, timeout, bad JSON, enrichment disabled — falls
back to the Phase 1 behaviour (first line as title, no tags), so the note is never lost and the
bot still works offline.

:func:`enrich_or_fallback` is the shared entry point: the document handler reuses it for the
text-like formats (`.txt` / `.csv`), which are notes whose body happens to come from a file. The
boundary it enforces is the same everywhere — **the body is always the original, the model only
supplies frontmatter.**
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import Settings
from ..engine import prompts
from ..engine.claude_cli import ClaudeCLI, ClaudeError, ClaudeUsageLimit, build_engine
from ..engine.parsing import ParseError, extract_json_object
from ..notes.markdown_writer import write_note
from .base import DeferMessage, HandlerResult, IncomingMessage

logger = logging.getLogger("contextbot.handlers.text")

_TITLE_MAX_LEN = 80
_SUMMARY_MAX_LEN = 200
_MAX_TAGS = 5
# Enrichment is a nicety, not the job: keep it well under the general engine budget so a slow
# model run can never stall the ingestion loop for a plain memo.
_ENRICH_TIMEOUT_SEC = 60.0

_SYSTEM_PROMPT = (
    "You are a metadata extractor for a personal Markdown knowledge base. "
    "You reply with a single JSON object and no other text."
)


@dataclass(frozen=True)
class Enrichment:
    """LLM-derived note metadata."""

    title: str
    tags: list[str]
    summary: str | None = None


def _derive_title(text: str) -> str:
    """Use the first non-empty line, trimmed, as the note title (the no-LLM fallback)."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            if len(stripped) > _TITLE_MAX_LEN:
                return stripped[:_TITLE_MAX_LEN].rstrip() + "…"
            return stripped
    return "Untitled note"


def clean_title(value: object) -> str | None:
    """Normalize a title candidate (any type), or None when nothing usable remains."""
    if not isinstance(value, str):
        return None
    title = value.strip().strip('"').rstrip(".")
    if not title:
        return None
    if len(title) > _TITLE_MAX_LEN:
        title = title[:_TITLE_MAX_LEN].rstrip() + "…"
    return title


def _clean_tags(value: object) -> list[str]:
    """Normalize the model's tags: strings only, lowercase, de-duplicated, capped."""
    if not isinstance(value, list):
        return []
    tags: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        tag = item.strip().lstrip("#").strip().lower().replace(" ", "-")
        if tag and tag not in tags:
            tags.append(tag)
    return tags[:_MAX_TAGS]


def _clean_summary(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    summary = " ".join(value.split())
    if not summary:
        return None
    if len(summary) > _SUMMARY_MAX_LEN:
        summary = summary[:_SUMMARY_MAX_LEN].rstrip() + "…"
    return summary


async def enrich(text: str, engine: ClaudeCLI) -> Enrichment | None:
    """Ask Claude for title/tags/summary. Returns None when the pass is unusable."""
    prompt = prompts.render("text_enrich", text=text)
    result = await engine.run(
        prompt, system_prompt=_SYSTEM_PROMPT, timeout_sec=_ENRICH_TIMEOUT_SEC
    )
    data = extract_json_object(result.text)

    title = clean_title(data.get("title"))
    if title is None:
        # Without a usable title there is nothing to gain over the first-line fallback.
        return None
    return Enrichment(
        title=title,
        tags=_clean_tags(data.get("tags")),
        summary=_clean_summary(data.get("summary")),
    )


async def enrich_or_fallback(
    text: str,
    settings: Settings,
    *,
    engine: ClaudeCLI | None = None,
    message_id: int | None = None,
) -> tuple[Enrichment | None, str | None]:
    """Enrich ``text``, returning ``(enrichment, degraded_reason)``.

    Returns ``(None, None)`` when enrichment is switched off (a deliberate choice, not a fault)
    and ``(None, reason)`` when it failed and the caller should fall back. The only exception it
    raises is :class:`DeferMessage`, for a usage limit — callers must therefore call this **before
    any side effect**, since a deferred message replays the handler from scratch.
    """
    if not settings.claude_enabled:
        return None, None

    engine = engine or build_engine(settings)
    try:
        return await enrich(text, engine), None
    except ClaudeUsageLimit as exc:
        # Transient and time-bound: defer rather than save a weaker note the owner would have
        # to find and fix later. Nothing is written yet, so the replay starts clean.
        logger.warning("usage limit reached; deferring message %s: %s", message_id, exc)
        raise DeferMessage(str(exc)) from exc
    except (ClaudeError, ParseError) as exc:
        # Expected failure modes (no CLI, timeout, non-JSON reply): degrade, don't fail.
        logger.warning("text enrichment unavailable, using fallback: %s", exc)
    except Exception:  # pragma: no cover - defensive: never lose a note to enrichment
        logger.exception("text enrichment raised unexpectedly, using fallback")
    return None, "LLM 보강에 실패했습니다"


async def handle_text(
    message: IncomingMessage,
    settings: Settings,
    *,
    engine: ClaudeCLI | None = None,
) -> HandlerResult:
    """Write the message text to a Markdown note and report the saved filename."""
    text = message.text.strip()
    if not text:
        return HandlerResult(reply="빈 메시지는 저장하지 않았습니다.")

    enrichment, degraded = await enrich_or_fallback(
        text, settings, engine=engine, message_id=message.message_id
    )

    title = enrichment.title if enrichment else _derive_title(text)
    tags = enrichment.tags if enrichment else []

    extra: dict[str, object] = {"telegram_message_id": message.message_id}
    if enrichment and enrichment.summary:
        extra["summary"] = enrichment.summary

    path = write_note(
        inbox_dir=settings.inbox_dir,
        body=text,
        title=title,
        when=message.date,
        source="telegram",
        note_type="note",
        tags=tags,
        extra=extra,
        slug_source=title,
    )
    # The bot DM is the only surface the owner sees, so a silent quality drop must still be
    # visible there — the note itself is saved either way.
    reply = f"📝 저장됨: {path.name}"
    if degraded:
        reply += f"\n⚠️ {degraded} (제목/태그는 기본값)"
    return HandlerResult(reply=reply, saved_path=path)
