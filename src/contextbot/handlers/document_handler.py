"""Document handler: a sent file becomes a Markdown note in the inbox.

Five routes, chosen by extension (docs/PHASE2.md, increment 2):

- **`.md`** → saved as-is; the file *is* the note. No conversion, no LLM.
- **`.txt`** → the file's text is the note body; the LLM only supplies title/tags/summary.
- **`.csv`** → rendered to a Markdown table *deterministically*, then handled as `.txt`.
- **`.pdf`** → Claude Code reads the PDF natively and returns Markdown. Original → `~/Downloads/`.
- **anything else** (docx/pptx/xlsx/…) → out of scope by decision: reply asking for a PDF export.

Two invariants run through all of it:

1. **The body is always the original.** For `.txt`/`.csv` the model never sees the note's content
   as something to rewrite — it returns frontmatter only. Only the `.pdf` route has the model
   produce body text, because there rendering the page *is* the job.
2. **Side effects come last.** A usage limit raises ``DeferMessage`` and the message is replayed
   from scratch on the next Start, so nothing may be written or moved until the last `claude -p`
   call has returned. Downloads go to a temp dir, which the replay simply redoes.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from ..config import Settings
from ..engine import prompts
from ..engine.claude_cli import ClaudeCLI, ClaudeError, ClaudeUsageLimit, build_engine
from ..files.originals import move_to_downloads
from ..files.text_files import DecodeError, decode_text, render_csv_table
from ..notes.markdown_writer import write_note, write_text
from ..notes.naming import build_filename
from .base import DeferMessage, HandlerResult, IncomingMessage, MessageKind
from .text_handler import clean_title, enrich_or_fallback

logger = logging.getLogger("contextbot.handlers.document")

_MARKDOWN_EXTS = {".md", ".markdown"}
_TEXT_LIKE_EXTS = {".txt", ".csv"}

# A note is a note, not a data dump: past this the file is unusable as one. The original is kept
# either way — in ~/Downloads for text-like files, and in Saved Messages forever.
_MAX_FILE_BYTES = 2_000_000
_MAX_CSV_ROWS = 200
_MAX_TEXT_CHARS = 50_000

# The model emits this instead of inventing content it could not read. Verified behaviour: blocked
# from its input, Claude read a *neighbouring* file with similar content, produced convincing
# Markdown, and reported success — so `is_error: False` is not proof of provenance. This sentinel
# and the isolated staging dir are the two things that make the PDF route trustworthy.
_SENTINEL = "CONVERSION_FAILED"

# The text path self-caps at 60s, but `Read` pages a PDF at ≤20 pages per call, so a long deck is
# several turns. Floor the budget well above that; CLAUDE_TIMEOUT_SEC still wins if raised higher.
_PDF_MIN_TIMEOUT_SEC = 300.0

# A real conversion opens with a `#` heading and a body. A reply shorter than this is a truncated
# or title-only stub that slipped past the sentinel (measured: a flaky run returned 19 chars) —
# treat it as a failed attempt so it is retried, not saved as a hollow note. Kept low so a genuinely
# brief document still converts; the retry and the original in ~/Downloads cover the rest.
_PDF_MIN_OUTPUT_CHARS = 20

# Conversion is flaky on large, image-heavy PDFs — the model sometimes takes the sentinel escape
# hatch or truncates on a file it can plainly read. A fresh retry clears that; a genuinely
# unreadable file returns the sentinel on every attempt and still ends in a reported failure, so
# the anti-fabrication guarantee holds. Bounded so a truly bad file can't burn the plan allowance.
_PDF_MAX_ATTEMPTS = 3

_PDF_SYSTEM_PROMPT = (
    "You are a document-to-Markdown converter for a personal knowledge base. "
    "You transcribe faithfully and never invent content you could not read."
)


def _ext(file_name: str | None) -> str:
    if not file_name or "." not in file_name:
        return ""
    return "." + file_name.rsplit(".", 1)[1].lower()


def _safe_name(file_name: str | None, message_id: int) -> str:
    """A filesystem-safe basename for a downloaded attachment.

    The name comes from Telegram, and a *forwarded* file's name is not the owner's own writing, so
    it is untrusted input: keep the basename only, so it cannot escape the temp dir.
    """
    name = Path(file_name or "").name.strip()
    if name in {"", ".", ".."}:
        return f"telegram-{message_id}"
    return name


async def _download(message: IncomingMessage, dest_dir: Path) -> Path:
    """Download the message's attachment into ``dest_dir`` and return its path.

    Raises on failure: a file we cannot download is a fault, so the client skips the message and
    tells the owner, rather than halting the bot on every Start.
    """
    download = getattr(message.raw, "download_media", None)
    if download is None:
        raise RuntimeError("message has no downloadable attachment")

    target = dest_dir / _safe_name(message.file_name, message.message_id)
    path = await download(file=str(target))
    if path is None:
        raise RuntimeError("Telegram returned no file for the attachment")
    return Path(path)


def _too_big_reply(name: str, size: int) -> str:
    return (
        f"📄 {name} 은(는) 너무 커서 노트로 만들지 않았습니다 "
        f"({size / 1_000_000:.1f}MB, 최대 {_MAX_FILE_BYTES / 1_000_000:.0f}MB)."
    )


# ------------------------------------------------------------------ .md passthrough
async def _handle_markdown(message: IncomingMessage, settings: Settings) -> HandlerResult:
    """Save a sent `.md` file straight into the inbox: it already *is* the note.

    The content is untouched (frontmatter included, if it has any). Only the filename is
    normalized to the vault's ``YYMMDD-HHMM-<slug>.md`` convention, so a sent note sorts into the
    inbox alongside the ones the bot writes.
    """
    with tempfile.TemporaryDirectory(prefix="contextbot-md-") as tmp:
        src = await _download(message, Path(tmp))
        raw = src.read_bytes()
        if len(raw) > _MAX_FILE_BYTES:
            return HandlerResult(reply=_too_big_reply(src.name, len(raw)))
        try:
            content = decode_text(raw)
        except DecodeError as exc:
            return HandlerResult(reply=f"📄 {src.name} 을(를) 읽지 못했습니다 — {exc}")

        path = write_text(
            directory=settings.inbox_dir,
            filename=build_filename(src.stem, message.date),
            content=content,
        )
    return HandlerResult(reply=f"📝 저장됨: {path.name}", saved_path=path)


# ------------------------------------------------------------------ .txt / .csv
def _body_for_text_like(src: Path, text: str) -> tuple[str, str | None]:
    """Return the note body for a text-like file, plus a notice if it had to be capped."""
    if src.suffix.lower() != ".csv":
        if len(text) > _MAX_TEXT_CHARS:
            return (
                text[:_MAX_TEXT_CHARS],
                f"내용이 길어 앞부분 {_MAX_TEXT_CHARS:,}자만 노트로 만들었습니다",
            )
        return text, None

    table = render_csv_table(text, max_rows=_MAX_CSV_ROWS)
    if table is None:
        return "", "빈 CSV 파일입니다"
    if not table.truncated:
        return table.markdown, None
    # Truncation is safe to make visible rather than fatal: the full file is in ~/Downloads.
    notice = f"전체 {table.total_rows:,}행 중 {table.shown_rows:,}행만 표시했습니다"
    return f"{table.markdown}\n\n*…{notice}. 원본은 Downloads 폴더에 있습니다.*", notice


async def _handle_text_like(
    message: IncomingMessage, settings: Settings, *, engine: ClaudeCLI | None
) -> HandlerResult:
    """`.txt` / `.csv` → a Markdown note. These are text-like, not PDF-like: no page, no `Read`.

    MarkNotes only surfaces `.md`, so passing these through would save something the owner can
    never find in the vault — hence a real note, with the file's content as the body.
    """
    with tempfile.TemporaryDirectory(prefix="contextbot-txt-") as tmp:
        src = await _download(message, Path(tmp))
        raw = src.read_bytes()

        if len(raw) > _MAX_FILE_BYTES:
            moved = move_to_downloads(src, downloads_dir=settings.downloads_dir)
            return HandlerResult(reply=f"{_too_big_reply(src.name, len(raw))}\n📎 원본: {moved}")
        try:
            text = decode_text(raw)
        except DecodeError as exc:
            # Permanent: the same bytes fail forever. Reply and move on — never defer.
            logger.warning("cannot decode %s: %s", src.name, exc)
            moved = move_to_downloads(src, downloads_dir=settings.downloads_dir)
            return HandlerResult(
                reply=f"📄 {src.name} 의 인코딩을 알 수 없어 노트로 만들지 못했습니다.\n📎 원본: {moved}"
            )

        body, notice = _body_for_text_like(src, text)
        if not body.strip():
            moved = move_to_downloads(src, downloads_dir=settings.downloads_dir)
            return HandlerResult(reply=f"📄 {src.name} 은(는) 비어 있습니다.\n📎 원본: {moved}")

        # Metadata only — the body above is already final and the model never rewrites it.
        # Before any side effect: this may raise DeferMessage.
        enrichment, degraded = await enrich_or_fallback(
            body, settings, engine=engine, message_id=message.message_id
        )

        extra: dict[str, object] = {
            "telegram_message_id": message.message_id,
            "original_file": src.name,
        }
        if enrichment and enrichment.summary:
            extra["summary"] = enrichment.summary

        title = enrichment.title if enrichment else src.stem
        path = write_note(
            inbox_dir=settings.inbox_dir,
            body=body,
            title=title,
            when=message.date,
            source="telegram",
            note_type="document",
            tags=enrichment.tags if enrichment else [],
            extra=extra,
            slug_source=title,
        )
        moved = move_to_downloads(src, downloads_dir=settings.downloads_dir)

    return HandlerResult(
        reply=_note_reply(path, moved, notice=notice, degraded=degraded), saved_path=path
    )


# ------------------------------------------------------------------ .pdf
def _conversion_failed(text: str) -> bool:
    """True when a conversion attempt did not produce a usable note.

    Covers three shapes: the explicit sentinel (checked on the first line, so a document that merely
    *mentions* the token still converts), empty output, and a stub too short to be a real document
    (a flaky truncation that would otherwise slip through as a hollow note).
    """
    stripped = text.strip()
    if len(stripped) < _PDF_MIN_OUTPUT_CHARS:
        return True
    first_line = stripped.splitlines()[0].strip().strip("`*# ")
    return first_line.startswith(_SENTINEL)


def _title_from_markdown(body: str, fallback: str) -> str:
    """Use the converted document's own `#` heading as the note title, else the filename."""
    for line in body.splitlines():
        if not line.strip():
            continue
        if line.strip().startswith("#"):
            title = clean_title(line.strip().lstrip("#"))
            if title:
                return title
        break  # only the document's leading heading counts
    return clean_title(fallback) or "Untitled document"


async def _handle_pdf(
    message: IncomingMessage, settings: Settings, *, engine: ClaudeCLI | None
) -> HandlerResult:
    """`.pdf` → Markdown, read natively by Claude Code. No converter, no shell, no dependency.

    The PDF is staged **alone** in a temp dir which is both the job's cwd and its only `--add-dir`.
    That isolation is not tidiness: given a file it cannot read, the model will happily convert a
    neighbouring one instead and report success. Staging alone removes the neighbours; the
    sentinel catches the rest. Conversion runs on ``claude_pdf_model`` (opus by default): a whole
    image-heavy document is measurably flakier than a text memo, and a bounded retry absorbs what
    the stronger model doesn't.
    """
    if not settings.claude_enabled:
        # No no-LLM path exists here — rendering the page *is* the LLM's job. Say so plainly
        # rather than saving an empty note.
        with tempfile.TemporaryDirectory(prefix="contextbot-pdf-") as tmp:
            src = await _download(message, Path(tmp))
            moved = move_to_downloads(src, downloads_dir=settings.downloads_dir)
        return HandlerResult(
            reply=f"📄 PDF 변환에는 Claude 엔진이 필요합니다 (CLAUDE_ENABLED=false).\n📎 원본: {moved}"
        )

    with tempfile.TemporaryDirectory(prefix="contextbot-pdf-") as tmp:
        stage = Path(tmp)
        src = await _download(message, stage)  # the only file in `stage`
        engine = engine or build_engine(settings)
        prompt = prompts.render("pdf_to_markdown", path=str(src), sentinel=_SENTINEL)
        timeout = max(settings.claude_timeout_sec, _PDF_MIN_TIMEOUT_SEC)

        body: str | None = None
        for attempt in range(1, _PDF_MAX_ATTEMPTS + 1):
            try:
                result = await engine.run(
                    prompt,
                    system_prompt=_PDF_SYSTEM_PROMPT,
                    add_dirs=[stage],
                    cwd=stage,
                    timeout_sec=timeout,
                    model=settings.claude_pdf_model,
                )
            except ClaudeUsageLimit as exc:
                # Transient: defer before anything is written or moved, and let the client halt.
                # The PDF stays in Saved Messages and the next Start replays it from scratch.
                logger.warning("usage limit; deferring message %s: %s", message.message_id, exc)
                raise DeferMessage(str(exc)) from exc
            except ClaudeError as exc:
                # A hard engine error (timeout, non-JSON) is not the flaky case a retry fixes.
                logger.warning("PDF conversion failed for %s: %s", src.name, exc)
                moved = move_to_downloads(src, downloads_dir=settings.downloads_dir)
                return HandlerResult(reply=_pdf_failed_reply(src.name, str(exc), moved))

            if not _conversion_failed(result.text):
                body = result.text
                break
            # is_error was False and the CLI reported success — but the reply is the sentinel or an
            # empty stub. On a readable file this is the model bailing spuriously, so retry; on a
            # genuinely unreadable one every attempt lands here and we fall through to the failure.
            logger.warning(
                "PDF conversion attempt %d/%d unusable for %s", attempt, _PDF_MAX_ATTEMPTS, src.name
            )

        if body is None:
            moved = move_to_downloads(src, downloads_dir=settings.downloads_dir)
            return HandlerResult(
                reply=_pdf_failed_reply(src.name, "문서를 읽지 못했습니다", moved)
            )

        title = _title_from_markdown(body, src.stem)
        path = write_note(
            inbox_dir=settings.inbox_dir,
            body=body,
            title=title,
            when=message.date,
            source="telegram",
            note_type="document",
            tags=[],
            extra={"telegram_message_id": message.message_id, "original_file": src.name},
            slug_source=title,
        )
        # Last, per the deferral rule: everything above is replayable, this is not.
        moved = move_to_downloads(src, downloads_dir=settings.downloads_dir)

    return HandlerResult(reply=_note_reply(path, moved), saved_path=path)


def _pdf_failed_reply(name: str, reason: str, moved: Path) -> str:
    return (
        f"⚠️ {name} 을(를) 마크다운으로 변환하지 못했습니다.\n"
        f"원인: {reason}\n"
        f"📎 원본: {moved}"
    )


def _note_reply(
    path: Path, moved: Path, *, notice: str | None = None, degraded: str | None = None
) -> str:
    reply = f"📝 저장됨: {path.name}\n📎 원본: {moved}"
    if notice:
        reply += f"\nℹ️ {notice}"
    if degraded:
        reply += f"\n⚠️ {degraded} (제목/태그는 기본값)"
    return reply


# ------------------------------------------------------------------ unsupported
def _unsupported_reply(name: str | None) -> str:
    return (
        f"📄 {name or '이 파일'} 형식은 지원하지 않습니다.\n"
        "MS Office에서 PDF로 내보내서 보내주세요 — 레이아웃까지 그대로 변환됩니다."
    )


async def handle_document(
    message: IncomingMessage,
    settings: Settings,
    *,
    engine: ClaudeCLI | None = None,
) -> HandlerResult:
    """Route a sent file to its pipeline by extension."""
    ext = _ext(message.file_name)

    if message.kind == MessageKind.MARKDOWN or ext in _MARKDOWN_EXTS:
        return await _handle_markdown(message, settings)
    if ext == ".pdf":
        return await _handle_pdf(message, settings, engine=engine)
    if ext in _TEXT_LIKE_EXTS:
        return await _handle_text_like(message, settings, engine=engine)

    # Not a failure and not a deferral: a .docx never becomes supported, so deferring would halt
    # the bot on every Start, and raising would fire a "처리 실패" DM for a bot that did exactly
    # what it should. A normal result advances the HWM, which is what stops the same file
    # re-nagging on every catch-up.
    logger.info("unsupported document format: %s", message.file_name)
    return HandlerResult(reply=_unsupported_reply(message.file_name))
