"""The meeting glossary: STT mis-transcription → the word that was actually said.

Why this exists, in one measured example. Asked to transcribe "티맵" (T-map), Whisper produced
**"팀웹"** — a plausible Korean word that is not the product's name, in a sentence that otherwise
reads perfectly. Nothing downstream can catch that: the note is fluent, confident, and wrong. The
glossary is the accumulated answer, and its first rows already carry `팀앱, 팀웨이, 팀웹 → T-map`.

It lives **in the vault** (`~/Documents/MarkNotes/.claude/contextbot/glossary.md` by default; see
`config.DEFAULT_GLOSSARY_PATH` for why that address and not this repo). This module only reads it
and appends to it — it does **not** commit or push. That is a decision, not an omission
(docs/PHASE2.md, increment 5, decision 3): the vault is already backed up wholesale by the owner,
and a `git push` from the poller's background task is a silent failure waiting on credentials.

The file's shape is meeting-transcriber's, unchanged, because the owner may still run `/meeting`
against the same table: a `## 용어 목록` heading over a four-column Markdown table.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("contextbot.files.glossary")

_HEADING = "## 용어 목록"
_COLUMNS = "| 전사 표현 (STT 출력) | 정확한 표현 | 유형 | 설명 |"
_SEPARATOR = "|-------------------|-----------|------|------|"

_NEW_FILE_HEADER = f"""# Meeting Glossary

STT 전사 오류 보정 및 확인된 용어 사전. 회의록을 만들 때 참조하고, 검토에서 확인된 용어가 여기에
추가된다.

{_HEADING}

{_COLUMNS}
{_SEPARATOR}
"""

# The kinds meeting-transcriber's SKILL.md classifies into. Anything else is normalised to 기타 so
# the table cannot grow a long tail of one-off categories.
KINDS = {"인명", "회사명", "제품명", "기능명", "기술용어", "팀명", "행사명", "단어오인식", "기타"}


@dataclass(frozen=True)
class GlossaryEntry:
    """One row: what STT wrote, what was meant, what kind of thing it is, and why."""

    transcribed: str
    correct: str
    kind: str = "기타"
    note: str = ""

    def as_row(self) -> str:
        return f"| {_cell(self.transcribed)} | {_cell(self.correct)} | {_cell(self.kind)} | {_cell(self.note)} |"


def _cell(text: str) -> str:
    """Make a value safe to sit in a Markdown table cell.

    A stray `|` would split the row into extra columns and silently corrupt the table for every
    future reader — including the model, which reads this file as its substitution list.
    """
    return " ".join(str(text).replace("|", "/").split()).strip()


def _is_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def _is_separator(line: str) -> bool:
    return _is_row(line) and set(line.strip()) <= set("|-: ")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def load_entries(path: Path) -> list[GlossaryEntry]:
    """Every term row in the glossary. Missing file → empty list, not an error."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("cannot read glossary at %s: %s", path, exc)
        return []

    entries: list[GlossaryEntry] = []
    for line in text.splitlines():
        if not _is_row(line) or _is_separator(line):
            continue
        cells = _cells(line)
        if len(cells) < 2 or not cells[0] or not cells[1]:
            continue
        if cells[0].startswith("전사 표현"):  # the column header
            continue
        entries.append(
            GlossaryEntry(
                transcribed=cells[0],
                correct=cells[1],
                kind=cells[2] if len(cells) > 2 else "기타",
                note=cells[3] if len(cells) > 3 else "",
            )
        )
    return entries


def known_terms(path: Path) -> set[str]:
    """Every transcribed form already covered, normalised for comparison.

    A row's first cell holds *several* comma-separated spellings (`재미나이, 제미나이`), so the unit
    of "already known" is the spelling, not the row.
    """
    terms: set[str] = set()
    for entry in load_entries(path):
        for form in entry.transcribed.split(","):
            normalised = form.strip().casefold()
            if normalised:
                terms.add(normalised)
    return terms


def append_entries(path: Path, entries: list[GlossaryEntry]) -> list[GlossaryEntry]:
    """Add the entries that are genuinely new; return exactly those. Never raises.

    Returning what was *written* rather than what was offered is what lets the reply tell the owner
    the truth ("2 added") instead of a number that counted duplicates.

    Rows go in after the **last existing table row**, not at end-of-file. Today the table happens to
    run to EOF, so `>> glossary.md` (what the `/meeting` skill does) works by luck of layout; the
    day a section is added below it, an EOF append would start writing rows outside the table and
    every future reader would quietly stop seeing them.
    """
    fresh: list[GlossaryEntry] = []
    seen = known_terms(path)
    for entry in entries:
        if not entry.transcribed.strip() or not entry.correct.strip():
            continue
        forms = {f.strip().casefold() for f in entry.transcribed.split(",") if f.strip()}
        if forms & seen:
            logger.debug("glossary already covers %r", entry.transcribed)
            continue
        fresh.append(entry)
        seen |= forms

    if not fresh:
        return []

    try:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_NEW_FILE_HEADER, encoding="utf-8")
        lines = path.read_text(encoding="utf-8").splitlines()
        cut = max((i for i, line in enumerate(lines) if _is_row(line)), default=len(lines) - 1) + 1
        merged = lines[:cut] + [e.as_row() for e in fresh] + lines[cut:]
        path.write_text("\n".join(merged) + "\n", encoding="utf-8")
    except OSError as exc:
        # The glossary is an upgrade, never a dependency: the owner's note is already written by the
        # time this runs, and failing to file a term must not turn an accepted note into an error.
        logger.warning("could not append to glossary at %s: %s", path, exc)
        return []

    logger.info("glossary: added %d entr(y/ies) to %s", len(fresh), path)
    return fresh


def parse_entries(text: str) -> list[GlossaryEntry]:
    """Read glossary rows out of a model's reply. Tolerant by design.

    The model is asked for a Markdown table, and "reply with a table and nothing else" is not a
    guarantee — the same assumption `engine/parsing.extract_json_object` exists for. Prose, fences,
    and a re-emitted header are all skipped rather than parsed into a junk row.
    """
    entries: list[GlossaryEntry] = []
    for line in text.splitlines():
        if not _is_row(line) or _is_separator(line):
            continue
        cells = _cells(line)
        if len(cells) < 2:
            continue
        transcribed, correct = cells[0], cells[1]
        if not transcribed or not correct:
            continue
        # The model likes to echo the column header back above its rows.
        if transcribed.startswith("전사") or correct.startswith("정확"):
            continue
        kind = cells[2] if len(cells) > 2 and cells[2] else "기타"
        entries.append(
            GlossaryEntry(
                transcribed=transcribed,
                correct=correct,
                kind=kind if kind in KINDS else "기타",
                note=cells[3] if len(cells) > 3 else "",
            )
        )
    return entries
