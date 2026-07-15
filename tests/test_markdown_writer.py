from datetime import datetime, timezone

import yaml

from contextbot.notes.markdown_writer import write_note


def _dt():
    return datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)


def test_write_note_creates_file(inbox):
    path = write_note(
        inbox_dir=inbox,
        body="본문입니다",
        title="테스트 노트",
        when=_dt(),
        extra={"telegram_message_id": 5},
    )
    assert path.exists()
    assert path.parent == inbox
    assert path.name == "260715-1430-테스트_노트.md"

    content = path.read_text(encoding="utf-8")
    assert "본문입니다" in content

    fm_text = content.split("---\n")[1]
    fm = yaml.safe_load(fm_text)
    assert fm["title"] == "테스트 노트"
    assert fm["telegram_message_id"] == 5


def test_write_note_creates_missing_inbox(tmp_path):
    target_dir = tmp_path / "new_inbox"
    assert not target_dir.exists()
    path = write_note(inbox_dir=target_dir, body="x", title="t", when=_dt())
    assert path.exists()
    assert path.parent == target_dir


def test_write_note_no_tmp_litter(inbox):
    write_note(inbox_dir=inbox, body="x", title="t", when=_dt())
    assert not list(inbox.glob("*.tmp"))


def test_write_note_collision(inbox):
    p1 = write_note(inbox_dir=inbox, body="a", title="같은 제목", when=_dt())
    p2 = write_note(inbox_dir=inbox, body="b", title="같은 제목", when=_dt())
    assert p1 != p2
    assert p2.name.endswith("-2.md")
