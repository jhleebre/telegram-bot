"""Tests for the shared attachment-download helper.

Extracted from `document_handler` in increment 3 so the image pipeline (and increment 5's audio)
use one implementation rather than three. The untrusted-filename handling is the part worth
pinning: the name comes from Telegram, and a forwarded file's name is not the owner's writing.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from contextbot.handlers.base import IncomingMessage, MessageKind
from contextbot.handlers.downloads import download_attachment, safe_name

DATE = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)


class FakeUpload:
    def __init__(self, *, returns: str | None = None):
        self._returns = returns
        self.asked: list[str] = []

    async def download_media(self, file):
        self.asked.append(file)
        if self._returns is None:
            return None
        Path(file).write_bytes(b"data")
        return file


def _msg(file_name, raw=None) -> IncomingMessage:
    return IncomingMessage(
        user_id=42,
        chat_id=9,
        message_id=7,
        date=DATE,
        kind=MessageKind.DOCUMENT,
        file_name=file_name,
        raw=raw,
    )


@pytest.mark.parametrize(
    "given,expected",
    [
        ("report.pdf", "report.pdf"),
        # A path is stripped to its basename: it must not escape the staging dir.
        ("../../etc/passwd", "passwd"),
        ("/absolute/evil.sh", "evil.sh"),
        ("subdir/photo.png", "photo.png"),
        # Nothing usable → a name derived from the message id.
        (None, "telegram-7"),
        ("", "telegram-7"),
        ("..", "telegram-7"),
        (".", "telegram-7"),
    ],
)
def test_safe_name(given, expected):
    assert safe_name(given, 7) == expected


async def test_download_writes_into_the_staging_dir(tmp_path: Path):
    upload = FakeUpload(returns="ok")
    path = await download_attachment(_msg("report.pdf", upload), tmp_path)

    assert path == tmp_path / "report.pdf"
    assert path.read_bytes() == b"data"


async def test_a_traversing_name_cannot_escape_the_staging_dir(tmp_path: Path):
    """The staging dir's isolation is what stops the model fabricating from a neighbour, so a
    file that writes itself outside it would undo the guarantee."""
    stage = tmp_path / "stage"
    stage.mkdir()

    path = await download_attachment(_msg("../../../evil.png", FakeUpload(returns="ok")), stage)

    assert path.parent == stage


async def test_a_message_without_an_attachment_raises(tmp_path: Path):
    with pytest.raises(RuntimeError):
        await download_attachment(_msg("a.pdf", object()), tmp_path)


async def test_a_download_that_returns_nothing_raises(tmp_path: Path):
    """Telethon returns None rather than raising when it declines to download."""
    with pytest.raises(RuntimeError):
        await download_attachment(_msg("a.pdf", FakeUpload(returns=None)), tmp_path)


async def test_the_path_telethon_returns_wins(tmp_path: Path):
    """Telethon appends the media's real extension when the target has none (a Telegram *photo*
    has no file name at all), and returns the adjusted path — so the caller must use the return
    value, not the path it asked for."""

    class Renaming:
        async def download_media(self, file):
            actual = Path(file).with_suffix(".jpg")
            actual.write_bytes(b"jpg")
            return str(actual)

    path = await download_attachment(_msg(None, Renaming()), tmp_path)

    assert path == tmp_path / "telegram-7.jpg"
