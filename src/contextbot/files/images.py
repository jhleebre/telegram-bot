"""Image format normalization: make a sent image readable by both consumers.

An image the bot ingests has to satisfy **two** independent readers, and they agree on the answer:

- **Claude's ``Read`` tool**, which renders the image for the model. Measured against the real CLI
  (v2.1.187): it renders ``png`` / ``jpg`` / ``jpeg`` / ``gif`` / ``webp``, and does **not** render
  ``heic`` / ``bmp``.
- **MarkNotes**, which renders the embed in the vault. Its ``ALLOWED_IMAGE_EXTENSIONS`` are
  ``.jpg .jpeg .png .gif .svg .webp`` — the same set, minus the two.

So the formats that need converting are the same for both: **heic/heif/bmp → png**.

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

Conversion uses **``sips``**, which ships with macOS: no new dependency, and this app is macOS-only
already (the UI is a native Qt app and increment 5's STT is Apple-Silicon ``mlx-whisper``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger("contextbot.files.images")

# Rendered as an image by both Claude's Read and MarkNotes — staged as-is, no re-encode.
VISION_READABLE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# Readable by neither. Converted to PNG before anything else touches them.
TRANSCODE_EXTS = {".heic", ".heif", ".bmp"}

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


async def normalize(src: Path, dest_dir: Path) -> Path:
    """Return a path to ``src`` in a renderable format, converting into ``dest_dir`` if needed.

    Returns ``src`` untouched when it is already renderable — re-encoding a PNG would only lose
    quality and time. Otherwise converts to PNG (lossless, and the safest input for OCR).

    Raises :class:`ImageError` when conversion is impossible or produces nothing. That is a
    *permanent* failure for these bytes, never a deferral: the caller writes a stub note that
    still embeds the original, so the capture survives.
    """
    if not needs_transcode(src):
        return src

    ext = src.suffix.lower()
    if ext not in TRANSCODE_EXTS:
        # An extension we have not measured. Try anyway rather than refusing outright: sips reads
        # far more formats than we list, and the stub-note path catches it if this fails.
        logger.info("unmeasured image format %s; attempting conversion", ext)

    sips = _resolve_sips()
    if sips is None:
        raise ImageError(f"{ext} 이미지를 변환할 수 없습니다 (sips를 찾지 못했습니다)")

    dest = dest_dir / f"{src.stem}.png"
    proc = await asyncio.create_subprocess_exec(
        sips,
        "-s",
        "format",
        "png",
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
        raise ImageError(f"{ext} 이미지를 PNG로 변환하지 못했습니다 — {detail or 'sips 실패'}")

    logger.info("converted %s → %s", src.name, dest.name)
    return dest
