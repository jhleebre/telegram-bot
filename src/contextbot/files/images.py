"""Image format normalization and inline embedding.

An image the bot ingests has to satisfy **two** independent readers, and they agree on the answer:

- **Claude's ``Read`` tool**, which renders the image for the model. Measured against the real CLI
  (v2.1.187): it renders ``png`` / ``jpg`` / ``jpeg`` / ``gif`` / ``webp``, and does **not** render
  ``heic`` / ``bmp``.
- **MarkNotes**, which renders the note. Its ``ALLOWED_IMAGE_EXTENSIONS`` are
  ``.jpg .jpeg .png .gif .svg .webp`` and its ``getImageMimeType`` maps exactly those — the same
  set, minus the two.

So the formats that need converting are the same for both: **heic/heif → jpeg, bmp → png**.

**Why this module exists at all** (the increment-3 finding, and the reason it is not optional):
asked to read a ``.heic``, ``Read`` does not fail. It returns the file's **raw bytes**, and the
model — seeing ``ftypheic`` in the header — replied *"a HEIC image, likely a photo taken on an
Apple device"* with ``is_error: False``. That is a confident, plausible, entirely unseen
description, and the note would have been saved and trusted. It is the same fabrication failure
increment 2 hit with the ``.docx``, wearing a different hat: **the model describes what it can
infer when it cannot see, and reports success either way.** Converting before the call is what
removes the question.

A ``.heic`` is the realistic input here — it is what an iPhone sends when the owner picks
"send as file" instead of a compressed photo.

**Why the conversion target depends on the source.** The note *embeds the image inline* (base64),
so the converted size is the note's size. Measured on a 12MP photo: a 1.85MB ``.heic`` becomes an
**18.3MB PNG** — a 24MB note — but a **4.0MB JPEG**, a 5.3MB note. A camera photo is already
lossy, so re-encoding it losslessly buys nothing and costs 10x. A ``.bmp`` is the opposite case:
uncompressed screen content, where PNG is both lossless and far smaller.

Conversion uses **``sips``**, which ships with macOS: no new dependency, and this app is macOS-only
already (the UI is a native Qt app and increment 5's STT is Apple-Silicon ``mlx-whisper``).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger("contextbot.files.images")

# Rendered as an image by both Claude's Read and MarkNotes — embedded as-is, never re-encoded.
VISION_READABLE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# Readable by neither. Converted before anything else touches them, to the closest renderable
# equivalent: a lossy camera photo becomes a JPEG, lossless screen content becomes a PNG.
TRANSCODE_TARGETS = {
    ".heic": "jpeg",
    ".heif": "jpeg",
    ".bmp": "png",
}
# Anything else unrenderable falls back to PNG: it cannot make an already-lossy file worse, and
# the size blowup only bites on photographs, which are the heic case above.
_DEFAULT_TARGET = "png"

_TARGET_SUFFIX = {"jpeg": ".jpg", "png": ".png"}

# MarkNotes' own getImageMimeType, so an embedded data URL is one it renders.
_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}

_SIPS = "sips"
# sips lives in /usr/bin, which is on launchd's minimal PATH, so a Dock-launched app finds it
# where it could not find `claude` (see PHASE2.md, "Finding the CLI when launched from the Dock").
# The explicit fallback costs one line and removes the assumption.
_SIPS_FALLBACK = "/usr/bin/sips"

_TRANSCODE_TIMEOUT_SEC = 60.0


class ImageError(Exception):
    """The image could not be normalized into a format the model and the vault can render."""


def _resolve_sips() -> str | None:
    found = shutil.which(_SIPS)
    if found:
        return found
    return _SIPS_FALLBACK if os.access(_SIPS_FALLBACK, os.X_OK) else None


def needs_transcode(src: Path) -> bool:
    """True when ``src``'s format is not renderable and must be converted first."""
    return src.suffix.lower() not in VISION_READABLE_EXTS


def mime_type(src: Path) -> str:
    """The MIME type for a data URL, per MarkNotes' own mapping."""
    return _MIME_TYPES.get(src.suffix.lower(), "application/octet-stream")


def to_data_url(src: Path) -> str:
    """Return ``src`` as a ``data:<mime>;base64,…`` URL, for embedding straight into a note.

    This is how the image gets *into* the Markdown rather than beside it. MarkNotes supports both
    forms, and the embedded one is what this bot writes: it needs no ``.assets/`` file and no entry
    in that folder's ``.metadata.json`` reference-tracking, so a note the bot wrote is complete and
    self-contained the moment it lands.
    """
    encoded = base64.b64encode(src.read_bytes()).decode("ascii")
    return f"data:{mime_type(src)};base64,{encoded}"


async def normalize(src: Path, dest_dir: Path) -> Path:
    """Return a path to ``src`` in a renderable format, converting into ``dest_dir`` if needed.

    Returns ``src`` untouched when it is already renderable — re-encoding it would only lose
    quality and inflate the note that embeds it.

    Raises :class:`ImageError` when conversion is impossible or produces nothing. That is a
    *permanent* failure for these bytes, never a deferral.
    """
    if not needs_transcode(src):
        return src

    ext = src.suffix.lower()
    target = TRANSCODE_TARGETS.get(ext)
    if target is None:
        # An extension we have not measured. Try anyway rather than refusing outright: sips reads
        # far more formats than we list, and the failure path catches it if this does not work.
        logger.info("unmeasured image format %s; attempting conversion", ext)
        target = _DEFAULT_TARGET

    sips = _resolve_sips()
    if sips is None:
        raise ImageError(f"{ext} 이미지를 변환할 수 없습니다 (sips를 찾지 못했습니다)")

    dest = dest_dir / f"{src.stem}{_TARGET_SUFFIX[target]}"
    proc = await asyncio.create_subprocess_exec(
        sips,
        "-s",
        "format",
        target,
        str(src),
        "--out",
        str(dest),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TRANSCODE_TIMEOUT_SEC)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:  # pragma: no cover - exited between check and kill
                pass
        raise ImageError(f"{ext} 이미지 변환이 시간 내에 끝나지 않았습니다") from None

    # sips reports some failures with a zero exit code and no output file, so check the file.
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[:200]
        raise ImageError(f"{ext} 이미지를 {target.upper()}로 변환하지 못했습니다 — {detail or 'sips 실패'}")

    logger.info(
        "converted %s → %s (%.1fMB → %.1fMB)",
        src.name,
        dest.name,
        src.stat().st_size / 1e6,
        dest.stat().st_size / 1e6,
    )
    return dest
