"""When a recording says, in its own metadata, what time it was where it was made.

A meeting note is dated by `message.date` — when the recording was *sent to Saved Messages*. That is
a stand-in for the thing the note actually claims, which is when the meeting *happened*. It is a good
stand-in most days (record it, send it) and a bad one whenever the two drift: a meeting recorded at
14:30 and sent that evening gets an Overview table saying 20:10, and a recording made abroad gets the
owner's home clock. This module is the narrow case where the file can settle it for us.

**Only tags carrying an explicit UTC offset are read, and that is the whole design.** The rule sounds
strict until you look at what the alternatives are actually worth:

- `com.apple.quicktime.creationdate` → `2026-07-17T14:30:00+0900`. **The one tag that answers the
  question.** The offset is the recording device's own, so it says *both* the instant and whose
  clock, which is exactly "where was this recorded" in the only form that survives a file transfer.
  iOS writes it (Voice Memos, camera); it lives in the QuickTime `mdta` box, so a re-encoder drops
  it rather than inventing one — its presence is evidence, not decoration.
- `creation_time` → `2026-07-17T05:30:00.000000Z`. **Rejected**, and not for pedantry: the MP4 spec
  defines it as UTC and ffprobe normalizes it to `Z`, so it repeats the instant we already have from
  Telegram and knows nothing about where. Worse, it is what *every* encoder stamps — ffmpeg writes
  the transcode time by default — so on a re-encoded file it is a confident lie about the recording
  time. Trusting it would trade a knowably-wrong date for an unknowably-wrong one.
- ID3 `TDRC` / `date` → `2026-07-17T14:30:00`. **Rejected**: no offset, and ID3v2.4 says timestamps
  are UTC while half the tools in the world write local time into it. Unresolvable ambiguity.
- GPS (`com.apple.quicktime.location.ISO6709`). Coordinates would need a lat/lon→zone lookup
  (`timezonefinder` and its map data) to become an offset — a real dependency for no gain, since
  Apple writes it *alongside* `creationdate`, which already carries the answer outright.

So: an explicit offset or nothing. Nothing is a fine answer — the caller falls back to the
configured zone, which is the right guess for a bot whose owner is in Seoul.

**The common case is `None`, by a wide margin.** A Telegram *voice message* is re-encoded to Opus in
an Ogg container and arrives with no metadata whatsoever (measured: not a single tag). This only pays
off for a recording sent as a **file** — which is how a real meeting recording arrives anyway, since
voice messages are for memos.

`ffprobe` is already a hard requirement of this project (mlx-whisper shells out to ffmpeg), so this
adds no dependency. It is still treated as optional at runtime: a missing or broken probe returns
`None` and the note gets the configured zone. Nothing here is worth failing a meeting note over.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("contextbot.files.audio_meta")

# Generous enough for a cold disk read of a large file's header, short enough that a wedged probe
# cannot hold up a pipeline whose answer we are prepared to do without.
_PROBE_TIMEOUT_SEC = 20.0

# Ordered by trust. Every entry must be a tag that carries a real UTC offset — see the module
# docstring before adding one. `creation_time` does not qualify and must never be added.
_OFFSET_TAGS = (
    "com.apple.quicktime.creationdate",
)


def _parse_offset_aware(raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, keeping it **only** if it carries a real UTC offset.

    Apple writes `+0900` (no colon), which `fromisoformat` rejects before Python 3.11 and accepts
    after; normalizing the two-digit form ourselves keeps the answer the same on every supported
    interpreter rather than depending on the runtime's parser generation.
    """
    text = raw.strip()
    if not text:
        return None
    # `+0900` / `-0400` → `+09:00` / `-04:00`, leaving an already-colonised offset alone.
    if len(text) >= 5 and text[-5] in "+-" and text[-4:].isdigit():
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    # A naive timestamp is the ambiguous case this module refuses to guess at.
    return value if value.tzinfo is not None else None


async def _probe_tags(path: Path) -> dict[str, str]:
    """Every format-level tag ffprobe can see, or an empty mapping if it cannot tell us."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, FileNotFoundError):
        # No ffprobe on PATH. Possible in principle (Whisper would also be broken), and not our
        # problem to report: the health panel owns that, and this route has a fallback.
        logger.info("ffprobe is unavailable; dating %s by the configured zone", path.name)
        return {}

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_PROBE_TIMEOUT_SEC)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:  # pragma: no cover - exited between check and kill
                pass
        logger.warning("ffprobe timed out on %s; dating it by the configured zone", path.name)
        return {}

    if proc.returncode != 0:
        return {}
    try:
        payload = json.loads(stdout or b"{}")
    except json.JSONDecodeError:
        return {}
    tags = payload.get("format", {}).get("tags", {})
    return tags if isinstance(tags, dict) else {}


async def recorded_at(path: Path) -> datetime | None:
    """When and *on whose clock* this file was recorded, or None if it does not say.

    The returned datetime is offset-aware and carries the **recording device's own** offset, so
    rendering it with `strftime` prints the wall clock a person in the room would have read. Callers
    must not re-convert it to a configured zone — that would throw away the only thing it knows.
    """
    tags = await _probe_tags(path)
    if not tags:
        return None

    # Tag keys are case-insensitive in practice across containers; normalize once rather than
    # guessing which case this particular muxer chose.
    lowered = {key.lower(): value for key, value in tags.items() if isinstance(value, str)}
    for tag in _OFFSET_TAGS:
        raw = lowered.get(tag.lower())
        if raw is None:
            continue
        value = _parse_offset_aware(raw)
        if value is not None:
            logger.info("%s says it was recorded at %s (%s)", path.name, value.isoformat(), tag)
            return value
        logger.debug("%s carried an unparseable %s: %r", path.name, tag, raw)
    return None
