"""The clock a note is written on.

The bug these guard against did not look like a bug: `strftime` on Telethon's UTC `message.date`
produces a perfectly well-formed timestamp that is simply nine hours from the truth. So the
assertions here are all about the *rendered wall clock*, which is the only place the fault was ever
visible.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from contextbot.localtime import as_local

SEOUL = ZoneInfo("Asia/Seoul")
NEW_YORK = ZoneInfo("America/New_York")


def test_utc_is_rendered_on_the_seoul_clock():
    """The reported symptom: 14:30 in Seoul was captioned 05:30."""
    utc = datetime(2026, 7, 17, 5, 30, tzinfo=timezone.utc)
    assert as_local(utc, SEOUL).strftime("%Y-%m-%d %H:%M") == "2026-07-17 14:30"


def test_the_instant_is_preserved():
    """Only the clock changes. A conversion that moved the instant would be a worse bug."""
    utc = datetime(2026, 7, 17, 5, 30, tzinfo=timezone.utc)
    assert as_local(utc, SEOUL) == utc


def test_an_early_morning_capture_keeps_its_own_date():
    """The filename half of the bug: 08:00 KST is the *previous* day in UTC, so a note captured
    before 09:00 was filed under yesterday (`YYMMDD-` comes from this datetime)."""
    utc = datetime(2026, 7, 17, 23, 0, tzinfo=timezone.utc)  # 08:00 KST on the 18th
    assert as_local(utc, SEOUL).strftime("%y%m%d") == "260718"


def test_naive_is_read_as_utc_not_as_this_machines_zone():
    """Otherwise the suite's answer would depend on the laptop it runs on, and `TZ=` would move it."""
    naive = datetime(2026, 7, 17, 5, 30)
    assert as_local(naive, SEOUL).strftime("%H:%M") == "14:30"


def test_it_is_idempotent():
    """Load-bearing: `route` applies it as a blanket floor and the audio pipeline re-derives on top.
    If a second application shifted the value, those two would fight."""
    utc = datetime(2026, 7, 17, 5, 30, tzinfo=timezone.utc)
    once = as_local(utc, SEOUL)
    assert as_local(once, SEOUL) == once
    assert as_local(once, SEOUL).strftime("%H:%M") == once.strftime("%H:%M")


def test_a_zone_other_than_seoul_is_honoured():
    """NOTE_TIMEZONE exists because the owner travels; a hardcoded +09:00 would be the same bug."""
    utc = datetime(2026, 7, 17, 5, 30, tzinfo=timezone.utc)
    assert as_local(utc, NEW_YORK).strftime("%Y-%m-%d %H:%M") == "2026-07-17 01:30"


def test_dst_is_the_zones_problem_not_ours():
    """A fixed offset would get this wrong half the year; ZoneInfo does not."""
    winter = datetime(2026, 1, 17, 17, 0, tzinfo=timezone.utc)
    summer = datetime(2026, 7, 17, 17, 0, tzinfo=timezone.utc)
    assert as_local(winter, NEW_YORK).strftime("%H:%M") == "12:00"  # EST, -5
    assert as_local(summer, NEW_YORK).strftime("%H:%M") == "13:00"  # EDT, -4
