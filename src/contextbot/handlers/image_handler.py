"""Image handler: a sent image becomes a note that embeds it, described and transcribed.

One route (docs/PHASE2.md, increment 3). A Telegram *photo*, or an image sent as a file
(png/jpg/jpeg/gif/webp/bmp/heic/heif), becomes a note in the inbox holding the **embedded original**
above a Claude-generated description + OCR. One-shot VLM call, no review loop (that is increment 4).

What makes this route different from every increment-2 one:

1. **The original is kept, not filed away.** It moves *into* the vault's ``.assets/`` — the note
   embeds it, so the image is the note's content, not a leftover. See ``files/originals.py``.
2. **A failure still produces a note.** A PDF without its conversion is nothing, so that route
   writes no note. An image without its description is still the image, and the owner sent it to
   keep it — so a non-limit failure writes a **stub note that embeds it anyway** and says the
   description is missing. The capture is never lost; only the searchable text is.
3. **The format has to be normalized first.** ``Read`` does not render heic/bmp — it returns raw
   bytes and lets the model describe the *file header* instead of the picture, reporting success.
   ``files/images.normalize`` converts those before the model is ever asked. See that module.

The invariants from increments 1–2 still bind: the isolated staging dir (the model fabricates from
a neighbour rather than admitting it cannot read something), the sentinel (``is_error: False`` is
not proof), and **every side effect after the last LLM call** (a usage limit raises
``DeferMessage`` and the message is replayed from scratch).
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from ..config import Settings
from ..engine import prompts
from ..engine.claude_cli import ClaudeCLI, ClaudeError, ClaudeUsageLimit, build_engine
from ..files.images import ImageError, normalize
from ..files.originals import embed_link, move_into_assets
from ..notes.markdown_writer import write_note
from ..notes.naming import build_filename, slugify
from .base import DeferMessage, HandlerResult, IncomingMessage
from .downloads import download_attachment
from .text_handler import clean_title

logger = logging.getLogger("contextbot.handlers.image")

# The image is stored as a file and embedded by reference, so the note does not grow with it —
# which is why this is far more generous than the document routes' 2MB. The number is MarkNotes'
# own MAX_IMAGE_SIZE: past it, the vault refuses to embed the image when exporting to PDF, so a
# larger file would be one the note could not fully use anyway.
_MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Describing one image is a small job next to converting a deck, but Read renders the image over
# a couple of turns; keep the floor above the text path's 60s without inheriting the PDF's 300s.
_IMAGE_MIN_TIMEOUT_SEC = 120.0

_SENTINEL = "DESCRIPTION_FAILED"

# A real description carries both `##` sections. Anything this short is a stub that slipped past
# the sentinel — the same guard the PDF route needs, for the same reason.
_MIN_OUTPUT_CHARS = 20

_SYSTEM_PROMPT = (
    "You are an image describer for a personal knowledge base. "
    "You describe only what you can actually see, and never invent content you were not shown."
)


def _description_failed(text: str) -> bool:
    """True when a description attempt did not produce anything usable.

    The sentinel is matched on the **first line**, not by substring: a screenshot of an error-code
    table that happens to contain the token must still produce its note.
    """
    stripped = text.strip()
    if len(stripped) < _MIN_OUTPUT_CHARS:
        return True
    first_line = stripped.splitlines()[0].strip().strip("`*# ")
    return first_line.startswith(_SENTINEL)


_TITLE_PREFIX = "제목:"


def _split_title(text: str) -> tuple[str | None, str]:
    """Pull the model's ``제목:`` line off the front of its reply; return (title, body).

    The title is asked for as a line rather than scraped from the description, because the first
    sentence of a description is a *sentence* — it made a 60-character title cut off mid-word, and
    a filename to match. A missing or malformed line is not a failure: the body is still good, so
    fall back rather than discard a perfectly usable description.
    """
    stripped = text.strip()
    first, _, rest = stripped.partition("\n")
    if not first.strip().startswith(_TITLE_PREFIX):
        return None, stripped

    title = clean_title(first.strip()[len(_TITLE_PREFIX) :])
    return title, rest.strip()


def _title_for(message: IncomingMessage, model_title: str | None) -> str:
    """The model's title, else the filename, else a default.

    A photo's filename is usually noise (``IMG_4821``), so the model wins when it gave a title.
    """
    if model_title:
        return model_title
    if message.file_name:
        title = clean_title(Path(message.file_name).stem)
        if title:
            return title
    return "이미지"


def _body(embed: str, description: str | None, reason: str | None) -> str:
    """The note body: the image first, then what we could say about it.

    The embed comes first either way — it is the part that is never missing.
    """
    if description:
        return f"{embed}\n\n{description}"
    return (
        f"{embed}\n\n"
        f"> ⚠️ 이미지 설명을 생성하지 못했습니다 — {reason}\n"
        "> 원본 이미지는 위에 그대로 남아 있습니다."
    )


async def _describe(
    image: Path, settings: Settings, *, engine: ClaudeCLI | None, stage: Path
) -> tuple[str | None, str]:
    """Run the one-shot VLM call; return (title, description).

    Raises ClaudeUsageLimit (defer) or ClaudeError (degrade to a stub note).
    """
    engine = engine or build_engine(settings)
    prompt = prompts.render("image_describe", path=str(image), sentinel=_SENTINEL)
    result = await engine.run(
        prompt,
        system_prompt=_SYSTEM_PROMPT,
        add_dirs=[stage],
        cwd=stage,
        timeout_sec=max(settings.claude_timeout_sec, _IMAGE_MIN_TIMEOUT_SEC),
        model=settings.claude_image_model,
    )
    if _description_failed(result.text):
        raise ClaudeError("이미지를 읽지 못했습니다")
    return _split_title(result.text)


async def handle_image(
    message: IncomingMessage,
    settings: Settings,
    *,
    engine: ClaudeCLI | None = None,
) -> HandlerResult:
    """A sent image → a note embedding it, with a description + OCR beneath."""
    with tempfile.TemporaryDirectory(prefix="contextbot-img-") as tmp:
        stage = Path(tmp)
        src = await download_attachment(message, stage)

        size = src.stat().st_size
        if size > _MAX_IMAGE_BYTES:
            # Permanent, so it replies and advances rather than deferring — but the image is still
            # kept and embedded, because that is the part worth saving.
            reason = (
                f"파일이 너무 큽니다 ({size / 1_000_000:.1f}MB, "
                f"최대 {_MAX_IMAGE_BYTES / 1_000_000:.0f}MB)"
            )
            return _write(message, settings, src, description=None, reason=reason)

        # Normalize before the model sees it: `Read` renders neither heic nor bmp, and given one it
        # describes the file's bytes rather than admitting it cannot see (increment-3 finding).
        # The converted file is what gets embedded too, not just what the model reads — MarkNotes'
        # ALLOWED_IMAGE_EXTENSIONS excludes exactly the same formats, so keeping the heic would
        # leave a note whose embed the vault cannot render either.
        try:
            readable = await normalize(src, stage)
        except ImageError as exc:
            logger.warning("cannot normalize %s: %s", src.name, exc)
            return _write(message, settings, src, description=None, reason=str(exc))

        if not settings.claude_enabled:
            return _write(
                message,
                settings,
                readable,
                description=None,
                reason="Claude 엔진이 꺼져 있습니다 (CLAUDE_ENABLED=false)",
            )

        model_title: str | None = None
        try:
            model_title, description = await _describe(
                readable, settings, engine=engine, stage=stage
            )
            reason: str | None = None
        except ClaudeUsageLimit as exc:
            # Transient: defer *before* the image is moved or the note written, so the replay
            # starts from a clean slate. The image stays in Saved Messages, which is durable.
            logger.warning("usage limit; deferring message %s: %s", message.message_id, exc)
            raise DeferMessage(str(exc)) from exc
        except ClaudeError as exc:
            # Non-limit: no fallback exists for a description, but the image is still worth
            # keeping — write the stub note rather than losing the capture.
            logger.warning("image description failed for %s: %s", src.name, exc)
            description, reason = None, str(exc)

        # Everything below is a side effect, so nothing above it may be one.
        return _write(
            message,
            settings,
            readable,
            description=description,
            reason=reason,
            model_title=model_title,
        )


def _write(
    message: IncomingMessage,
    settings: Settings,
    image: Path,
    *,
    description: str | None,
    reason: str | None,
    model_title: str | None = None,
) -> HandlerResult:
    """Move the image into the vault and write the note that embeds it. Side effects live here."""
    title = _title_for(message, model_title)
    assets_dir = settings.assets_dir or (settings.inbox_dir.parent / ".assets")

    # Name the image after the note it belongs to, so the two sort together in `.assets/`.
    stem = build_filename(title, message.date, extension="").rstrip(".")
    stored = move_into_assets(
        image, assets_dir=assets_dir, filename=f"{stem}{image.suffix.lower()}"
    )

    path = write_note(
        inbox_dir=settings.inbox_dir,
        body=_body(embed_link(stored), description, reason),
        title=title,
        when=message.date,
        source="telegram",
        note_type="image",
        tags=[],
        extra={
            "telegram_message_id": message.message_id,
            "original_file": message.file_name or stored.name,
            "image": f".assets/{stored.name}",
        },
        slug_source=slugify(title),
    )

    reply = f"🖼️ 저장됨: {path.name}\n📎 이미지: {stored.name}"
    if reason:
        reply += f"\n⚠️ 설명 없이 저장했습니다 — {reason}"
    return HandlerResult(reply=reply, saved_path=path, extra_paths=[stored])
