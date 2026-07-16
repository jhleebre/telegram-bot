"""Image normalization tests.

These drive the **real `sips`** (a local, offline macOS binary — no network, no model) on real
image bytes, for the same reason `test_claude_cli.py` drives a real subprocess: the whole module
exists because of a measured fact about which formats render, and a fully mocked test would assert
our *assumptions* about `sips` rather than its behaviour. The conversions are small and fast.
"""

import asyncio
import base64
from pathlib import Path

import pytest

from contextbot.files.images import (
    VISION_READABLE_EXTS,
    ImageError,
    mime_type,
    needs_transcode,
    normalize,
    to_data_url,
)

# A real 8x8 opaque RGB PNG. Not 1x1, and not RGBA: sips refuses to *write* a BMP from either
# ("Error 13"), so a smaller fixture would fail in the fixture rather than in the code under test.
_PNG_8X8 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000080000000808020000004b6d29dc"
    "0000006c49444154789c0dc9410100300803319ce0a44eea84c7f9c0094eea66cb"
    "375545172a5c4cb1c51529aa9a6ed4b899669b6bd23f440b098b112b4e443f4c1b"
    "199b316bcec43f861e34789861871b323f965eb4789965975bb23f8e3e74f89863"
    "8f3b723f42070587091b2e243c3b5b56418b7c63ba0000000049454e44ae426082"
)

pytestmark = pytest.mark.skipif(
    not Path("/usr/bin/sips").exists(), reason="sips is macOS-only; the app is a macOS app"
)


def _png(tmp_path: Path, name: str = "shot.png") -> Path:
    path = tmp_path / name
    path.write_bytes(_PNG_8X8)
    return path


async def _to(tmp_path: Path, src: Path, fmt: str, name: str) -> Path:
    """Convert with sips directly, to produce a real fixture in a format we do not write."""
    dest = tmp_path / name
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/sips", "-s", "format", fmt, str(src), "--out", str(dest),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    return dest


@pytest.mark.parametrize("ext", sorted(VISION_READABLE_EXTS))
def test_readable_formats_need_no_transcode(ext):
    """Measured against the real CLI: Read renders each of these, and MarkNotes embeds them."""
    assert not needs_transcode(Path(f"a{ext}"))


@pytest.mark.parametrize("ext", [".heic", ".heif", ".bmp"])
def test_unreadable_formats_need_a_transcode(ext):
    """Read hands these back as raw *bytes* without erroring, so the model describes the file
    header and reports success. Converting first is what removes that failure mode."""
    assert needs_transcode(Path(f"a{ext}"))


def test_extension_case_does_not_matter():
    assert not needs_transcode(Path("IMG_4821.PNG"))
    assert needs_transcode(Path("IMG_4821.HEIC"))


async def test_a_readable_image_is_returned_untouched(tmp_path: Path):
    src = _png(tmp_path)
    assert await normalize(src, tmp_path) == src  # same path: not re-encoded, not copied


async def test_heic_is_converted_to_a_real_jpeg(tmp_path: Path):
    """The iPhone case: what an owner sends when they pick 'send as file'.

    JPEG, not PNG, because the note *embeds* the result: measured on a 12MP photo, a 1.85MB heic
    becomes an 18.3MB PNG (a 24MB note) but a 4.0MB JPEG. It is already a lossy camera photo, so
    re-encoding it losslessly costs 10x and buys nothing.
    """
    heic = await _to(tmp_path, _png(tmp_path), "heic", "IMG_4821.heic")
    assert heic.is_file()  # guard: the fixture itself is real

    out = tmp_path / "out"
    out.mkdir()
    result = await normalize(heic, out)

    assert result == out / "IMG_4821.jpg"
    assert result.read_bytes().startswith(b"\xff\xd8\xff")  # a genuine JPEG, not a renamed heic


async def test_bmp_is_converted_to_png(tmp_path: Path):
    """The opposite case from a photo: uncompressed screen content, where PNG is lossless *and*
    smaller than the source."""
    bmp = await _to(tmp_path, _png(tmp_path), "bmp", "scan.bmp")
    out = tmp_path / "out"
    out.mkdir()

    result = await normalize(bmp, out)

    assert result == out / "scan.png"
    assert result.read_bytes().startswith(b"\x89PNG")


# ------------------------------------------------------------------ embedding
def test_to_data_url_round_trips_the_bytes(tmp_path: Path):
    src = _png(tmp_path)

    url = to_data_url(src)

    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == _PNG_8X8


@pytest.mark.parametrize(
    "name,expected",
    [
        ("a.png", "image/png"),
        ("a.jpg", "image/jpeg"),
        ("a.jpeg", "image/jpeg"),
        ("a.JPG", "image/jpeg"),
        ("a.gif", "image/gif"),
        ("a.webp", "image/webp"),
        ("a.svg", "image/svg+xml"),
    ],
)
def test_mime_types_match_marknotes(name, expected):
    """The data URL declares the type MarkNotes renders by, so this mirrors its getImageMimeType."""
    assert mime_type(Path(name)) == expected


async def test_a_corrupt_image_raises_rather_than_producing_nothing(tmp_path: Path):
    """A stub note beats a note describing a file nobody could open."""
    broken = tmp_path / "broken.heic"
    broken.write_bytes(b"not actually an image")

    with pytest.raises(ImageError):
        await normalize(broken, tmp_path)


async def test_a_missing_sips_raises_rather_than_passing_the_image_through(
    tmp_path: Path, monkeypatch
):
    """Passing an unconverted heic on to the model is the one thing that must never happen."""
    monkeypatch.setattr("contextbot.files.images._resolve_sips", lambda: None)

    with pytest.raises(ImageError):
        await normalize(tmp_path / "IMG.heic", tmp_path)
