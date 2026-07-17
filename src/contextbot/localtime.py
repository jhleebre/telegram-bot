"""Turning an instant into the wall clock a note should show.

**The bug this exists to kill.** Telethon hands us `message.date` as UTC-aware, and every render
site used to format it as-is: `strftime` on a UTC datetime prints the *UTC* wall clock. A meeting
recorded at 14:30 in Seoul was captioned `2026-07-17 05:30` in the note's Overview table — nine
hours off, and stated with total confidence. The same instant also named the file (`YYMMDD`) and
filled `date:` in the frontmatter, so a note captured before 09:00 KST was **filed under the
previous day**.

The distinction the old code missed is that a datetime carries two separable facts: *which instant*
and *whose clock*. UTC was never wrong about the instant — it was answering a question nobody asked.
`as_local` picks the clock.

**Naive input is read as UTC, not as this machine's zone.** `astimezone()` on a naive datetime
silently assumes system local time, which would make the bot's output depend on the laptop's
settings and quietly differ between the developer's machine and a test runner. Telethon is always
aware, so a naive value here means a fake or a hand-built message — and UTC is the assumption every
other date-handling seam in the project already makes (see `session_store._aware`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def as_local(value: datetime, tz: ZoneInfo) -> datetime:
    """``value`` as the same instant on ``tz``'s clock.

    Idempotent, and deliberately so: calling it twice with the same zone is a no-op, which is what
    lets `route` apply it as a blanket floor while the audio pipeline re-derives its own answer on
    top without either having to know about the other.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz)
