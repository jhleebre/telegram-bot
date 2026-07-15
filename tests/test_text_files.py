import pytest

from contextbot.files.text_files import DecodeError, decode_text, render_csv_table


# ------------------------------------------------------------------ decode_text
def test_decodes_utf8():
    assert decode_text("회의 메모".encode("utf-8")) == "회의 메모"


def test_decodes_utf8_with_bom():
    assert decode_text("제목".encode("utf-8-sig")) == "제목"


def test_decodes_cp949():
    """Excel on Windows writes Korean CSV as CP949 — the landmine this exists for."""
    assert decode_text("이름,부서\n김철수,영업".encode("cp949")) == "이름,부서\n김철수,영업"


def test_decodes_euc_kr():
    """EUC-KR is a subset of CP949, so it decodes on the same attempt."""
    assert decode_text("한글".encode("euc-kr")) == "한글"


def test_decodes_utf16():
    assert decode_text("보고서".encode("utf-16")) == "보고서"


def test_undecodable_bytes_raise():
    with pytest.raises(DecodeError):
        decode_text(b"\x00\x01\x02\xff\xfe\xfd\x80\x81 \xc0\xc0\xc0")


def test_never_silently_mojibakes():
    """latin-1 would decode anything; using it would turn a reportable failure into garbage."""
    original = "매출 현황"
    assert decode_text(original.encode("cp949")) == original


# ------------------------------------------------------------- render_csv_table
def test_renders_a_markdown_table():
    table = render_csv_table("name,qty\napple,3\npear,5\n", max_rows=100)
    assert table.markdown == (
        "| name | qty |\n| --- | --- |\n| apple | 3 |\n| pear | 5 |"
    )
    assert table.total_rows == 2 and table.shown_rows == 2
    assert not table.truncated


def test_numbers_are_copied_verbatim():
    """The whole point of rendering deterministically: no model retypes the owner's figures."""
    table = render_csv_table("항목,금액\n매출,1234567.89\n", max_rows=100)
    assert "1234567.89" in table.markdown


def test_row_cap_truncates_and_reports():
    rows = "\n".join(f"r{i},{i}" for i in range(50))
    table = render_csv_table(f"a,b\n{rows}\n", max_rows=10)

    assert table.total_rows == 50 and table.shown_rows == 10
    assert table.truncated
    assert table.markdown.count("\n") == 11  # header + divider + 10 rows


def test_pipes_are_escaped_so_cells_cannot_break_out():
    table = render_csv_table('a,b\n"x|y",z\n', max_rows=10)
    assert r"x\|y" in table.markdown
    assert table.markdown.splitlines()[2] == r"| x\|y | z |"


def test_newlines_in_a_cell_become_br():
    table = render_csv_table('a,b\n"one\ntwo",z\n', max_rows=10)
    assert "one<br>two" in table.markdown
    assert len(table.markdown.splitlines()) == 3


def test_ragged_rows_are_padded_to_the_widest():
    table = render_csv_table("a,b\n1,2,3\n", max_rows=10)
    lines = table.markdown.splitlines()
    assert lines[0] == "| a | b |  |"  # no data is dropped: the extra column survives
    assert lines[2] == "| 1 | 2 | 3 |"


def test_semicolon_delimiter_is_sniffed():
    table = render_csv_table("name;qty\napple;3\npear;5\n", max_rows=10)
    assert table.markdown.splitlines()[2] == "| apple | 3 |"


def test_tab_delimiter_is_sniffed():
    table = render_csv_table("name\tqty\napple\t3\npear\t5\n", max_rows=10)
    assert table.markdown.splitlines()[2] == "| apple | 3 |"


def test_blank_lines_are_dropped():
    table = render_csv_table("a,b\n\n1,2\n\n", max_rows=10)
    assert table.total_rows == 1


def test_empty_csv_returns_none():
    assert render_csv_table("", max_rows=10) is None
    assert render_csv_table("\n\n", max_rows=10) is None


def test_header_only_csv_renders_an_empty_table():
    table = render_csv_table("a,b\n", max_rows=10)
    assert table.total_rows == 0
    assert table.markdown == "| a | b |\n| --- | --- |"
