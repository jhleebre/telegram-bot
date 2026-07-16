"""Downloading a message's attachment to disk — shared by every media pipeline.

Extracted from ``document_handler`` in increment 3: the image pipeline needs it too, and the
audio pipeline (increment 5) will be the third caller. It lives under ``handlers/`` rather than
``files/`` because it takes an :class:`IncomingMessage`; keeping it here preserves the
``handlers → files`` dependency direction.
"""

from __future__ import annotations

from pathlib import Path

from .base import IncomingMessage


def safe_name(file_name: str | None, message_id: int) -> str:
    """A filesystem-safe basename for a downloaded attachment.

    The name comes from Telegram, and a *forwarded* file's name is not the owner's own writing, so
    it is untrusted input: keep the basename only, so it cannot escape the temp dir.
    """
    name = Path(file_name or "").name.strip()
    if name in {"", ".", ".."}:
        return f"telegram-{message_id}"
    return name


async def download_attachment(message: IncomingMessage, dest_dir: Path) -> Path:
    """Download the message's attachment into ``dest_dir`` and return its path.

    Raises on failure: a file we cannot download is a fault, so the client skips the message and
    tells the owner, rather than halting the bot on every Start.
    """
    download = getattr(message.raw, "download_media", None)
    if download is None:
        raise RuntimeError("message has no downloadable attachment")

    target = dest_dir / safe_name(message.file_name, message.message_id)
    path = await download(file=str(target))
    if path is None:
        raise RuntimeError("Telegram returned no file for the attachment")
    return Path(path)
