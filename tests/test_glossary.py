"""Glossary tests: reading the table, appending only what is new, and never corrupting it.

The load-bearing one is negative: a wrong glossary entry silently mis-corrects every future meeting
note, so appending is the one place here that must be conservative.
"""

from pathlib import Path

import pytest

from contextbot.files.glossary import (
    GlossaryEntry,
    append_entries,
    known_terms,
    load_entries,
    parse_entries,
)

GLOSSARY = """# Meeting Glossary

## 용어 목록

| 전사 표현 (STT 출력) | 정확한 표현 | 유형 | 설명 |
|-------------------|-----------|------|------|
| 재미나이, 제미나이 | Google Gemini | 제품명 | Google AI 어시스턴트 |
| 팀앱, 팀웨이, 팀웹 | T-map | 제품명 | SK텔레콤 내비게이션 |
"""


@pytest.fixture
def glossary(tmp_path) -> Path:
    path = tmp_path / "glossary.md"
    path.write_text(GLOSSARY, encoding="utf-8")
    return path


# -------------------------------------------------------------------------- reading
def test_load_entries_reads_the_table(glossary):
    entries = load_entries(glossary)

    assert [e.correct for e in entries] == ["Google Gemini", "T-map"]
    assert entries[0].kind == "제품명"
    assert entries[1].transcribed == "팀앱, 팀웨이, 팀웹"


def test_load_entries_skips_the_header_and_separator(glossary):
    assert all("전사 표현" not in e.transcribed for e in load_entries(glossary))
    assert len(load_entries(glossary)) == 2


def test_a_missing_glossary_is_empty_not_an_error(tmp_path):
    """Absent is a valid state: no substitutions, note still made."""
    assert load_entries(tmp_path / "nope.md") == []
    assert known_terms(tmp_path / "nope.md") == set()


def test_known_terms_splits_the_comma_separated_forms(glossary):
    """One row covers several spellings, so the unit of "already known" is the spelling."""
    terms = known_terms(glossary)

    assert "팀웹" in terms
    assert "팀웨이" in terms
    assert "재미나이" in terms
    assert "t-map" not in terms  # the *correct* form is not a transcription


# ------------------------------------------------------------------------ appending
def test_append_adds_a_new_entry_to_the_table(glossary):
    added = append_entries(glossary, [GlossaryEntry("오픈클로", "OpenClaw", "제품명", "에이전트")])

    assert [e.correct for e in added] == ["OpenClaw"]
    assert "| 오픈클로 | OpenClaw | 제품명 | 에이전트 |" in glossary.read_text(encoding="utf-8")
    assert len(load_entries(glossary)) == 3


def test_append_skips_a_term_already_covered(glossary):
    """SKILL.md's rule: if the 전사 표현 is already there, do not add it again."""
    added = append_entries(glossary, [GlossaryEntry("팀웹", "T-map", "제품명", "중복")])

    assert added == []
    assert glossary.read_text(encoding="utf-8") == GLOSSARY


def test_append_returns_only_what_it_wrote(glossary):
    """The reply tells the owner this number, so it must not count the duplicates."""
    added = append_entries(
        glossary,
        [GlossaryEntry("팀웹", "T-map"), GlossaryEntry("에이다", "에이닷", "제품명")],
    )

    assert [e.correct for e in added] == ["에이닷"]


def test_append_deduplicates_within_one_batch(glossary):
    added = append_entries(
        glossary, [GlossaryEntry("새말", "New Word"), GlossaryEntry("새말", "New Word")]
    )
    assert len(added) == 1


def test_append_creates_the_file_when_there_is_none(tmp_path):
    """A fresh machine has no glossary; the first accepted meeting note starts one."""
    path = tmp_path / "nested" / "glossary.md"

    added = append_entries(path, [GlossaryEntry("팀웹", "T-map", "제품명")])

    assert len(added) == 1
    assert "## 용어 목록" in path.read_text(encoding="utf-8")
    assert load_entries(path)[0].correct == "T-map"


def test_append_lands_inside_the_table_not_at_end_of_file(tmp_path):
    """Rows go after the last table row, not at EOF.

    Today the table happens to run to EOF, so `>> glossary.md` works by luck of layout. The moment
    a section is added below it, an EOF append writes rows outside the table and every reader — the
    model included — quietly stops seeing them.
    """
    path = tmp_path / "glossary.md"
    path.write_text(GLOSSARY + "\n## 메모\n\n표 아래 섹션.\n", encoding="utf-8")

    append_entries(path, [GlossaryEntry("새말", "New Word")])

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines.index("| 새말 | New Word | 기타 |  |") < lines.index("## 메모")
    assert load_entries(path)[-1].correct == "New Word"


def test_append_escapes_a_pipe_so_the_table_survives(glossary):
    """A raw `|` splits the row into extra columns and corrupts the table for every future read."""
    append_entries(glossary, [GlossaryEntry("A|B", "C|D", "기타", "무|엇")])

    entry = load_entries(glossary)[-1]
    assert entry.transcribed == "A/B"
    assert entry.correct == "C/D"


def test_append_flattens_newlines_out_of_a_cell(glossary):
    append_entries(glossary, [GlossaryEntry("가\n나", "다\n라")])
    assert load_entries(glossary)[-1].transcribed == "가 나"


def test_append_ignores_an_empty_entry(glossary):
    assert append_entries(glossary, [GlossaryEntry("", "X"), GlossaryEntry("Y", "")]) == []
    assert glossary.read_text(encoding="utf-8") == GLOSSARY


def test_append_never_raises_on_an_unwritable_path(tmp_path):
    """The note is already written by the time this runs — a filing failure must not undo it."""
    path = tmp_path / "dir-not-file"
    path.mkdir()

    assert append_entries(path, [GlossaryEntry("가", "나")]) == []


# -------------------------------------------------------- parsing the model's reply
def test_parse_entries_reads_a_table_out_of_a_reply():
    entries = parse_entries(
        """확인된 용어는 다음과 같습니다:

| 전사 표현 | 정확한 표현 | 유형 | 설명 |
|---|---|---|---|
| 팀웹 | T-map | 제품명 | 내비 |
| 김지군 | 김지훈 | 인명 | |
"""
    )

    assert [(e.transcribed, e.correct, e.kind) for e in entries] == [
        ("팀웹", "T-map", "제품명"),
        ("김지군", "김지훈", "인명"),
    ]


def test_parse_entries_skips_prose_and_the_echoed_header():
    """"Reply with a table and nothing else" is not a guarantee — same lesson as extract_json_object."""
    assert parse_entries("확인된 용어가 없습니다.") == []
    assert parse_entries("NONE") == []


def test_parse_entries_normalises_an_unknown_kind():
    """Otherwise the table grows a long tail of one-off categories the model invented."""
    assert parse_entries("| 가 | 나 | 이상한분류 | |")[0].kind == "기타"


def test_parse_entries_needs_both_columns():
    assert parse_entries("| 가 | | 인명 | |") == []
    assert parse_entries("| | 나 | 인명 | |") == []
