from datetime import datetime, timezone

from contextbot.notes.naming import build_filename, slugify, unique_path


def _dt():
    return datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)


def test_slugify_basic():
    assert slugify("Hello World") == "hello_world"


def test_slugify_strips_punctuation():
    assert slugify("Meeting: Q3 plan!!!") == "meeting_q3_plan"


def test_slugify_keeps_korean():
    assert slugify("분기 목표 설정") == "분기_목표_설정"


def test_slugify_uses_first_line_only():
    assert slugify("first line\nsecond line") == "first_line"


def test_slugify_empty_fallback():
    assert slugify("   \n  ") == "note"
    assert slugify("!!!") == "note"


def test_slugify_truncates():
    long = "a" * 100
    assert len(slugify(long)) <= 40


def test_build_filename_format():
    name = build_filename("Hello there", _dt())
    assert name == "260715-1430-hello_there.md"


def test_unique_path_no_collision(tmp_path):
    p = unique_path(tmp_path, "260715-1430-note.md")
    assert p == tmp_path / "260715-1430-note.md"


def test_unique_path_collision(tmp_path):
    first = tmp_path / "260715-1430-note.md"
    first.write_text("x")
    second = unique_path(tmp_path, "260715-1430-note.md")
    assert second == tmp_path / "260715-1430-note-2.md"
    second.write_text("y")
    third = unique_path(tmp_path, "260715-1430-note.md")
    assert third == tmp_path / "260715-1430-note-3.md"
