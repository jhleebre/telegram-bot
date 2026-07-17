"""Reading a recording's own claim about when — and where — it was made.

**These build real files and run the real ffprobe.** The whole module is a bet about what survives
in a container and what a muxer normalizes away, and a mocked ffprobe would only ever confirm the
bet back to itself. The two facts worth having in a test are exactly the ones a fake cannot hold:
that Apple's `creationdate` keeps its `+0900` through a write/read round-trip, and that plain
`creation_time` does not — ffmpeg rewrites it to `Z` and the offset is gone forever.
"""

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contextbot.files.audio_meta import _parse_offset_aware, recorded_at

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="needs ffmpeg/ffprobe (a declared runtime dependency; brew install ffmpeg)",
)


def _encode(dest: Path, *metadata: str, movflags: bool = False) -> Path:
    """A one-second real m4a carrying `metadata`, written by the real ffmpeg."""
    args = ["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac"]
    for item in metadata:
        args += ["-metadata", item]
    if movflags:
        # Apple's keys live in the QuickTime `mdta` box, which ffmpeg only writes when asked.
        args += ["-movflags", "use_metadata_tags"]
    args += ["-y", str(dest)]
    subprocess.run(args, check=True)
    return dest


# ------------------------------------------------------------------ the real thing
async def test_an_apple_creationdate_gives_the_recordings_own_clock(tmp_path):
    """The case the whole module exists for: an iOS recording says what time it was *there*."""
    path = _encode(
        tmp_path / "memo.m4a",
        "com.apple.quicktime.creationdate=2026-07-17T14:30:00+0900",
        movflags=True,
    )
    stamped = await recorded_at(path)
    assert stamped is not None
    # Rendered as-is, this is the wall clock someone in the room read.
    assert stamped.strftime("%Y-%m-%d %H:%M") == "2026-07-17 14:30"
    assert stamped.utcoffset().total_seconds() == 9 * 3600


async def test_a_recording_made_abroad_keeps_the_local_clock(tmp_path):
    """Not Seoul-shaped: the offset is the *device's*, so a Berlin meeting is dated in Berlin."""
    path = _encode(
        tmp_path / "berlin.m4a",
        "com.apple.quicktime.creationdate=2026-07-17T09:15:00+0200",
        movflags=True,
    )
    stamped = await recorded_at(path)
    assert stamped.strftime("%Y-%m-%d %H:%M") == "2026-07-17 09:15"
    assert stamped.utcoffset().total_seconds() == 2 * 3600


# ------------------------------------------------- what we deliberately refuse to read
async def test_a_plain_creation_time_is_ignored(tmp_path):
    """It is spec'd UTC, ffprobe normalizes it to `Z`, and **every encoder stamps it** — ffmpeg
    writes the transcode time by default. It repeats the instant Telegram already told us and knows
    nothing about where, so reading it would swap a knowably-wrong date for an unknowably-wrong one."""
    path = _encode(tmp_path / "reencoded.m4a", "creation_time=2026-07-17T05:30:00.000000Z")
    assert await recorded_at(path) is None


async def test_an_id3_date_without_an_offset_is_ignored(tmp_path):
    """`2026-07-17T14:30:00` is unresolvable: ID3v2.4 says UTC, half the world writes local time."""
    path = tmp_path / "memo.mp3"
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "libmp3lame", "-metadata", "TDRC=2026-07-17T14:30:00", "-y", str(path)],
        check=True,
    )
    assert await recorded_at(path) is None


async def test_a_telegram_voice_message_says_nothing(tmp_path):
    """The overwhelmingly common input, and the reason the fallback is not a corner case: Telegram
    re-encodes voice to Opus and the file arrives with no tags at all."""
    path = tmp_path / "voice.oga"
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "libopus", "-y", str(path)],
        check=True,
    )
    assert await recorded_at(path) is None


# ------------------------------------------------------------------ not-a-recording
async def test_garbage_bytes_do_not_raise(tmp_path):
    """A note is worth more than this answer: anything unreadable falls back rather than failing."""
    path = tmp_path / "junk.m4a"
    path.write_bytes(b"not an audio file")
    assert await recorded_at(path) is None


async def test_a_missing_file_does_not_raise(tmp_path):
    assert await recorded_at(tmp_path / "nope.m4a") is None


# ------------------------------------------------------------------ the parser alone
@pytest.mark.parametrize(
    "raw, expected_offset_hours",
    [
        ("2026-07-17T14:30:00+0900", 9),      # Apple's colon-less form
        ("2026-07-17T14:30:00+09:00", 9),     # the colonised form
        ("2026-07-17T09:15:00-0400", -4),
    ],
)
def test_offsets_parse_in_both_spellings(raw, expected_offset_hours):
    """Apple writes `+0900`, which `fromisoformat` rejected before 3.11 — normalized by hand so the
    answer does not depend on which interpreter generation is running."""
    value = _parse_offset_aware(raw)
    assert value is not None
    assert value.utcoffset().total_seconds() == expected_offset_hours * 3600


@pytest.mark.parametrize(
    "raw",
    [
        "2026-07-17T14:30:00",        # naive: the ambiguity we refuse to guess at
        "2026-07-17",                 # a date is not a time
        "",
        "   ",
        "garbage",
        "0000-00-00T00:00:00+0900",   # a real thing broken encoders write
    ],
)
def test_unusable_stamps_are_declined(raw):
    assert _parse_offset_aware(raw) is None


def test_a_z_suffix_is_parsed_but_carries_no_locale():
    """`Z` is offset-aware, so the parser keeps it — the *tag allowlist* is what keeps it out, not
    this function. Documenting the split so a future edit does not 'fix' the wrong half."""
    value = _parse_offset_aware("2026-07-17T05:30:00Z")
    assert value == datetime(2026, 7, 17, 5, 30, tzinfo=timezone.utc)
