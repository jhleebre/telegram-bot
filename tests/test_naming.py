from datetime import datetime, timezone

import pytest

from contextbot.notes.naming import (
    DOCUMENT_CATEGORIES,
    NOTE_CATEGORY,
    build_filename,
    normalize_category,
    slugify,
    unique_path,
)


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
    """The vault's convention: YYMMDD-<분류>-<slug>. The clock is gone from the name."""
    assert build_filename("Hello there", _dt(), category="회의") == "260715-회의-hello_there.md"


def test_unique_path_no_collision(tmp_path):
    p = unique_path(tmp_path, "260715-노트-note.md")
    assert p == tmp_path / "260715-노트-note.md"


def test_unique_path_collision(tmp_path):
    first = tmp_path / "260715-노트-note.md"
    first.write_text("x")
    second = unique_path(tmp_path, "260715-노트-note.md")
    assert second == tmp_path / "260715-노트-note-2.md"
    second.write_text("y")
    third = unique_path(tmp_path, "260715-노트-note.md")
    assert third == tmp_path / "260715-노트-note-3.md"


# ------------------------------------------------- the vault's category slot
def test_build_filename_carries_the_category():
    assert build_filename("주간 회의", _dt(), category="회의") == "260715-회의-주간_회의.md"
    assert build_filename("H2 전략", _dt(), category="전략") == "260715-전략-h2_전략.md"


def test_build_filename_requires_a_category():
    """No default, on purpose: the category is a claim about what the note *is*, and every route
    knows its own answer. A default would let a new route silently inherit someone else's."""
    with pytest.raises(TypeError):
        build_filename("x", _dt())


@pytest.mark.parametrize("category", ["전략", "기획", "조사", "안건", "보고", "초안"])
def test_the_six_document_categories_are_the_vaults_own(category):
    """Every one is already in use there — 전략 40x, 조사 12x, 보고 3x, 안건 2x, 초안 1x, 기획 1x."""
    assert category in DOCUMENT_CATEGORIES
    assert normalize_category(category) == category


def test_an_invented_category_becomes_a_note():
    """The category lands in a *filename*, so an open set would let a model's improvisation name a
    file. 노트 is the honest answer for "we could not tell"."""
    assert normalize_category("전략적 분석") == NOTE_CATEGORY
    assert normalize_category("Strategy") == NOTE_CATEGORY
    assert normalize_category("") == NOTE_CATEGORY
    assert normalize_category(None) == NOTE_CATEGORY


def test_a_category_can_never_smuggle_a_path_separator():
    """It is interpolated straight into a filename."""
    assert normalize_category("보고/기획") == NOTE_CATEGORY
    assert normalize_category("../../etc/passwd") == NOTE_CATEGORY


def test_a_decomposed_category_is_still_that_category():
    """The vault already holds one `전략` written as decomposed jamo (NFD) beside 40 composed ones.

    So "the model said 전략" and "the string equals 전략" are not the same question — without NFC
    normalization an NFD answer silently falls back to 노트, and no one could see why by reading it.
    """
    import unicodedata

    nfd = unicodedata.normalize("NFD", "전략")
    assert nfd != "전략"  # different bytes, identical on screen
    assert normalize_category(nfd) == "전략"


def test_category_normalization_tolerates_surrounding_space():
    assert normalize_category("  보고 ") == "보고"
