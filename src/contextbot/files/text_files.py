"""Turning an uploaded text-like file (`.txt` / `.csv` / `.md`) into note body text.

Everything here is deterministic and LLM-free, on purpose. A `.csv` is rendered to a Markdown
table by the stdlib `csv` module rather than transcribed by the model: asking an LLM to retype the
owner's numbers invites a silently altered figure in a data file, which is exactly the error
nothing downstream can catch. The model's job stays metadata-only (see docs/PHASE2.md).
"""

from __future__ import annotations

import codecs
import csv
import io
from dataclasses import dataclass

# Excel on Windows writes Korean CSV as CP949, not UTF-8, so a plain read_text() would raise or
# mojibake. Tried in order; the first that decodes cleanly wins. Deliberately *not* including
# latin-1 as a last resort: it decodes any byte sequence, which would turn a hard failure (which
# we report) into silent mojibake (which we would not).
_ENCODINGS = ("utf-8-sig", "cp949")

_BOM_UTF16 = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)


class DecodeError(Exception):
    """The file is not text in any encoding we support.

    Permanent, never transient: the same bytes fail identically forever, so the caller replies and
    moves on rather than deferring the message.
    """


def decode_text(raw: bytes) -> str:
    """Decode file bytes to text, trying the encodings the owner's tools actually produce."""
    if raw.startswith(_BOM_UTF16):  # Excel's "Unicode Text" export
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DecodeError(f"not valid text in any of: utf-8, {', '.join(_ENCODINGS[1:])}, utf-16")


@dataclass(frozen=True)
class CsvTable:
    """A CSV rendered as a Markdown table, plus how much of it was kept."""

    markdown: str
    total_rows: int  # data rows in the file, excluding the header
    shown_rows: int  # data rows actually rendered

    @property
    def truncated(self) -> bool:
        return self.shown_rows < self.total_rows


def _dialect(sample: str) -> type[csv.Dialect] | csv.Dialect:
    """Sniff the delimiter; fall back to plain comma-separated when the sniff is inconclusive."""
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def _cell(value: str) -> str:
    """Escape a value so it cannot break out of its table cell."""
    text = value.strip().replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("|", r"\|").replace("\n", "<br>")


def _row(cells: list[str], width: int) -> str:
    padded = [_cell(c) for c in cells] + [""] * (width - len(cells))
    return "| " + " | ".join(padded) + " |"


def render_csv_table(text: str, *, max_rows: int) -> CsvTable | None:
    """Render CSV text as a Markdown table, keeping at most ``max_rows`` data rows.

    Returns None when the file holds no rows. The first row is taken as the header. Ragged rows
    are padded to the width of the *widest* row, so a stray extra column is never dropped.
    """
    reader = csv.reader(io.StringIO(text), _dialect(text[:4096]))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return None

    width = max(len(row) for row in rows)
    header, data = rows[0], rows[1:]
    shown = data[:max_rows]

    lines = [_row(header, width), "| " + " | ".join(["---"] * width) + " |"]
    lines += [_row(row, width) for row in shown]
    return CsvTable(markdown="\n".join(lines), total_rows=len(data), shown_rows=len(shown))
