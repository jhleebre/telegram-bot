from datetime import datetime, timezone

import yaml

from contextbot.notes.frontmatter import (
    build_frontmatter,
    render_frontmatter,
    render_note,
)


def _dt():
    return datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)


def test_build_frontmatter_defaults():
    fm = build_frontmatter(title="Hello", date=_dt())
    assert fm["title"] == "Hello"
    assert fm["source"] == "telegram"
    assert fm["type"] == "note"
    assert fm["tags"] == []
    assert fm["date"] == _dt().isoformat()


def test_build_frontmatter_extra_and_tags():
    fm = build_frontmatter(
        title="T", date=_dt(), tags=["a", "b"], extra={"telegram_message_id": 7}
    )
    assert fm["tags"] == ["a", "b"]
    assert fm["telegram_message_id"] == 7


def test_render_frontmatter_is_valid_yaml_block():
    fm = build_frontmatter(title="한글 제목", date=_dt(), tags=["메모"])
    block = render_frontmatter(fm)
    assert block.startswith("---\n")
    assert block.rstrip().endswith("---")
    parsed = yaml.safe_load(block.strip().strip("-"))
    assert parsed["title"] == "한글 제목"
    assert parsed["tags"] == ["메모"]


def test_render_note_structure():
    fm = build_frontmatter(title="T", date=_dt())
    note = render_note(fm, "본문 내용\n둘째 줄")
    assert note.startswith("---\n")
    assert "본문 내용" in note
    # Body should be separated from frontmatter and end with a single trailing newline.
    assert note.endswith("둘째 줄\n")
    front, _, body = note.partition("---\n")[2].partition("---\n")
    assert "본문 내용" in body
