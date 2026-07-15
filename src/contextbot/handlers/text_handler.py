"""Text handler: a text memo becomes a Markdown note in the inbox.

Phase 2 adds an optional LLM enrichment pass (`claude -p` → title / tags / summary). It is
strictly an *upgrade*: any failure — CLI missing, timeout, bad JSON, enrichment disabled — falls
back to the Phase 1 behaviour (first line as title, no tags), so the note is never lost and the
bot still works offline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import Settings
from ..engine import prompts
from ..engine.claude_cli import ClaudeCLI, ClaudeError, build_engine
from ..engine.parsing import ParseError, extract_json_object
from ..notes.markdown_writer import write_note
from .base import HandlerResult, IncomingMessage

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


def _clean_title(value: object) -> str | None:
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

    title = _clean_title(data.get("title"))
    if title is None:
        # Without a usable title there is nothing to gain over the first-line fallback.
        return None
    return Enrichment(
        title=title,
        tags=_clean_tags(data.get("tags")),
        summary=_clean_summary(data.get("summary")),
    )


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

    enrichment: Enrichment | None = None
    if settings.claude_enabled:
        engine = engine or build_engine(settings)
        try:
            enrichment = await enrich(text, engine)
        except (ClaudeError, ParseError) as exc:
            # Expected failure modes (no CLI, timeout, non-JSON reply): degrade, don't fail.
            logger.warning("text enrichment unavailable, using fallback: %s", exc)
        except Exception:  # pragma: no cover - defensive: never lose a note to enrichment
            logger.exception("text enrichment raised unexpectedly, using fallback")

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
    return HandlerResult(reply=f"📝 저장됨: {path.name}", saved_path=path)
