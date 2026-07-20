"""Image handler: a sent image becomes a note that embeds it, described and transcribed.

One route (docs/PHASE2.md, increment 3). A Telegram *photo*, or an image sent as a file
(png/jpg/jpeg/gif/webp/bmp/heic/heif), becomes a note in the inbox holding the **embedded original**
above a Claude-generated description + OCR. One-shot VLM call, no review loop (that is increment 4).

What makes this route different from every increment-2 one:

1. **The image goes *inside* the note**, base64-encoded into the Markdown
   (``![…](data:image/jpeg;base64,…)``). There is no original left over to file away: the note is
   the storage. MarkNotes supports this alongside its ``.assets/`` folder, and the embedded form is
   the one to write — an ``.assets/`` image is only half the story, because that folder's
   ``.metadata.json`` tracks which notes reference which file, and a bot writing files in behind
   the app's back would leave that ledger wrong. Embedding sidesteps the ledger entirely: a note
   the bot wrote is complete and self-contained the moment it lands. (Sharing one file across
   notes is what ``.assets/`` buys, and this vault is text-first — the reuse never happens.)
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
from ..files.images import ImageError, normalize, to_data_url
from ..files.originals import move_to_downloads
from ..notes.markdown_writer import write_note
from ..notes.naming import NOTE_CATEGORY, slugify
from .base import DeferMessage, HandlerResult, IncomingMessage
from .downloads import download_attachment
from .text_handler import clean_title

logger = logging.getLogger("contextbot.handlers.image")

# MarkNotes' own MAX_IMAGE_SIZE, applied to the bytes that actually get embedded — i.e. **after**
# any conversion, which is the only measurement that means anything: a 1.85MB iPhone .heic becomes
# a 4MB JPEG, and would have become an 18MB PNG. Base64 inflates by a further ~4/3, so 10MB of
# image is a ~13MB note. Past this the vault refuses to embed it anyway, so there is nothing to be
# gained by writing one.
_MAX_EMBED_BYTES = 10 * 1024 * 1024

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
    """The model's title, else the caption, else the filename, else a default.

    A photo's filename is usually noise (``IMG_4821``), so the model wins when it gave a title.

    The caption sits second, and only reaches here on the stub-note path — no model ran, so nothing
    looked at the picture and the alternative is `IMG_4821` or the bare word 이미지. The owner's own
    words about the image beat both, even when they were phrased as an instruction rather than a
    label: "영수증 정리해줘" is a worse title than the model would have written and a much better one
    than `IMG_4821`, because it is the only thing on the note that says what the image was.
    """
    if model_title:
        return model_title
    if message.caption:
        # First line only: a multi-line caption is a paragraph, and a title is a label.
        title = clean_title(message.caption.strip().splitlines()[0])
        if title:
            return title
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
        "> 이미지는 이 노트 안에 그대로 들어 있습니다."
    )


def _undelivered_reply(name: str, reason: str, moved: Path) -> str:
    """No embeddable image means no note — so say why, and say where the file went."""
    return f"🖼️ {name} 을(를) 노트로 만들지 못했습니다.\n원인: {reason}\n📎 원본: {moved}"


async def _describe(
    image: Path, settings: Settings, *, engine: ClaudeCLI | None, stage: Path, caption: str = ""
) -> tuple[str | None, str]:
    """Run the one-shot VLM call; return (title, description).

    Raises ClaudeUsageLimit (defer) or ClaudeError (degrade to a stub note).
    """
    engine = engine or build_engine(settings)
    prompt = prompts.render(
        "image_describe",
        path=str(image),
        sentinel=_SENTINEL,
        caption=prompts.caption_section(caption),
    )
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
    """A sent image → a note with the image embedded in it, described and transcribed."""
    with tempfile.TemporaryDirectory(prefix="contextbot-img-") as tmp:
        stage = Path(tmp)
        src = await download_attachment(message, stage)

        # Normalize before the model sees it: `Read` renders neither heic nor bmp, and given one it
        # describes the file's bytes rather than admitting it cannot see (increment-3 finding).
        # The converted file is what gets embedded too, not just what the model reads — MarkNotes
        # renders exactly the same set, so an embedded heic would be a broken image in the vault.
        try:
            readable = await normalize(src, stage)
        except ImageError as exc:
            # No renderable image exists, so there is nothing to embed and a note would be empty.
            # File the original instead: permanent, so it replies and advances rather than defers.
            logger.warning("cannot normalize %s: %s", src.name, exc)
            moved = move_to_downloads(src, downloads_dir=settings.downloads_dir)
            return HandlerResult(reply=_undelivered_reply(src.name, str(exc), moved))

        # Checked *after* conversion, because that is what gets embedded: a .heic passes this on
        # its own size and then triples. Too big to embed = no note worth writing, so the original
        # is filed like any other, and the owner is told where it went.
        size = readable.stat().st_size
        if size > _MAX_EMBED_BYTES:
            reason = (
                f"이미지가 너무 커서 노트에 넣지 못했습니다 "
                f"({size / 1_000_000:.1f}MB, 최대 {_MAX_EMBED_BYTES / 1_000_000:.0f}MB)"
            )
            logger.info("image too large to embed: %s (%d bytes)", src.name, size)
            moved = move_to_downloads(src, downloads_dir=settings.downloads_dir)
            return HandlerResult(reply=_undelivered_reply(src.name, reason, moved))

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
                readable, settings, engine=engine, stage=stage, caption=message.caption
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
    """Write the note, with the image encoded into it. The only side effect in this handler.

    Embedding rather than linking is what makes that true: there is no file to move, so a note
    either exists complete or does not exist at all.
    """
    title = _title_for(message, model_title)
    embed = f"![{slugify(title)}]({to_data_url(image)})"

    extra: dict[str, object] = {"telegram_message_id": message.message_id}
    # Only when the owner actually sent a named file: a Telegram photo has no name, and recording
    # the temp file we invented for it would say nothing.
    if message.file_name:
        extra["original_file"] = message.file_name
    # Acting on a caption is lossy — it went into the description's wording rather than staying a
    # sentence. This is where the owner's own words survive.
    if message.caption:
        extra["caption"] = message.caption

    path = write_note(
        inbox_dir=settings.inbox_dir,
        category=NOTE_CATEGORY,
        body=_body(embed, description, reason),
        title=title,
        when=message.date,
        source="telegram",
        note_type="image",
        tags=[],
        extra=extra,
        slug_source=slugify(title),
    )

    reply = f"🖼️ 저장됨: {path.name}"
    if reason:
        reply += f"\n⚠️ 설명 없이 저장했습니다 — {reason}"
    return HandlerResult(reply=reply, saved_path=path)
