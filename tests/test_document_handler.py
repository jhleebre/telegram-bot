"""Document handler tests: `.md` / `.txt` / `.csv` / `.pdf` / unsupported.

No model is ever run: the engine is a fake that returns canned text. Downloads are fakes that
write bytes to the path the handler chose, which is what lets the isolation of the PDF staging
directory be asserted for real.
"""

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from contextbot.core.router import classify_document
from contextbot.engine.claude_cli import (
    ClaudeResult,
    ClaudeTimeout,
    ClaudeUnavailable,
    ClaudeUsageLimit,
)
from contextbot.handlers.base import DeferMessage, IncomingMessage
from contextbot.handlers.document_handler import handle_document

DATE = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)


class FakeUpload:
    """Stands in for a Telethon Message with a downloadable attachment."""

    def __init__(self, content: bytes):
        self.content = content
        self.downloads: list[str] = []

    async def download_media(self, file):
        Path(file).write_bytes(self.content)
        self.downloads.append(file)
        return file


def _msg(file_name: str | None, content: bytes = b"", *, message_id: int = 7) -> IncomingMessage:
    return IncomingMessage(
        user_id=42,
        chat_id=999,
        message_id=message_id,
        date=DATE,
        kind=classify_document(file_name, None),
        file_name=file_name,
        raw=FakeUpload(content),
    )


class FakeEngine:
    """Stands in for ClaudeCLI: records each call, returns canned text or raises.

    ``texts`` gives a different reply per successive call (for the PDF retry path); ``text`` is the
    single-reply shorthand.
    """

    def __init__(
        self, *, text: str = "", texts: list[str] | None = None, raises: Exception | None = None
    ):
        self._texts = texts if texts is not None else [text]
        self._raises = raises
        self.calls: list[dict] = []

    async def run(self, prompt: str, **kwargs):
        call = {"prompt": prompt, **kwargs}
        cwd = kwargs.get("cwd")
        if cwd is not None:  # what the model could actually see, captured at call time
            call["visible"] = sorted(p.name for p in Path(cwd).iterdir())
        self.calls.append(call)
        if self._raises is not None:
            raise self._raises
        # Clamp to the last reply so a fixed single-text engine answers every retry the same way.
        text = self._texts[min(len(self.calls) - 1, len(self._texts) - 1)]
        return ClaudeResult(text=text, session_id="s-1")


def _metadata(payload: dict) -> FakeEngine:
    return FakeEngine(text=json.dumps(payload, ensure_ascii=False))


def _fm(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---\n")[1])


def _body(path: Path) -> str:
    return path.read_text(encoding="utf-8").split("---\n", 2)[2]


def _downloads(settings) -> list[str]:
    d = settings.downloads_dir
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


@pytest.fixture
def llm_settings(settings):
    return replace(settings, claude_enabled=True)


# =================================================================== .md passthrough
async def test_markdown_is_saved_as_is(settings):
    content = "---\ntitle: 내가 쓴 노트\n---\n\n# 제목\n\n본문입니다.\n"
    result = await handle_document(_msg("idea.md", content.encode("utf-8")), settings)

    assert result.saved_path is not None
    # Byte-for-byte: no frontmatter injected, no conversion, nothing reformatted.
    assert result.saved_path.read_text(encoding="utf-8") == content
    assert "저장됨" in result.reply


async def test_markdown_filename_follows_the_vault_convention(settings):
    result = await handle_document(_msg("My Idea.md", b"# x"), settings)
    assert result.saved_path.name == "260715-노트-my_idea.md"


async def test_markdown_never_calls_the_engine(llm_settings):
    engine = FakeEngine(text='{"title": "should not be used"}')
    await handle_document(_msg("idea.md", b"# x"), llm_settings, engine=engine)
    assert engine.calls == []


async def test_markdown_original_is_not_moved_to_downloads(settings):
    """The `.md` file *is* the note — there is no leftover original to file away."""
    await handle_document(_msg("idea.md", b"# x"), settings)
    assert _downloads(settings) == []


async def test_markdown_collision_gets_a_suffix(settings):
    await handle_document(_msg("idea.md", b"# one"), settings)
    second = await handle_document(_msg("idea.md", b"# two"), settings)
    assert second.saved_path.name == "260715-노트-idea-2.md"


async def test_markdown_in_cp949(settings):
    result = await handle_document(_msg("메모.md", "# 한글 제목".encode("cp949")), settings)
    assert result.saved_path.read_text(encoding="utf-8") == "# 한글 제목"


async def test_oversized_markdown_is_refused(settings):
    result = await handle_document(_msg("big.md", b"x" * 2_000_001), settings)
    assert result.saved_path is None
    assert "너무 커서" in result.reply
    assert not list(settings.inbox_dir.iterdir())


# =================================================================== .txt
async def test_txt_body_is_the_file_text(settings):
    result = await handle_document(_msg("memo.txt", "첫 줄\n둘째 줄".encode("utf-8")), settings)

    assert "첫 줄\n둘째 줄" in _body(result.saved_path)
    assert _fm(result.saved_path)["telegram_message_id"] == 7
    assert _fm(result.saved_path)["original_file"] == "memo.txt"


async def test_txt_title_falls_back_to_the_filename(settings):
    result = await handle_document(_msg("회의 준비.txt", b"body"), settings)
    assert _fm(result.saved_path)["title"] == "회의 준비"


async def test_txt_enrichment_supplies_metadata_only(llm_settings):
    engine = _metadata({"title": "예산 검토", "tags": ["budget"], "summary": "요약."})
    result = await handle_document(
        _msg("memo.txt", "원본 내용".encode("utf-8")), llm_settings, engine=engine
    )

    fm = _fm(result.saved_path)
    assert fm["title"] == "예산 검토"
    assert fm["tags"] == ["budget"]
    assert fm["summary"] == "요약."
    # The body is the file, never the model's reply.
    assert "원본 내용" in _body(result.saved_path)
    assert "예산 검토" not in _body(result.saved_path)


async def test_txt_original_moves_to_downloads(settings):
    result = await handle_document(_msg("memo.txt", b"body"), settings)
    assert _downloads(settings) == ["memo.txt"]
    assert "원본" in result.reply


async def test_txt_in_cp949(settings):
    result = await handle_document(_msg("memo.txt", "한글 본문".encode("cp949")), settings)
    assert "한글 본문" in _body(result.saved_path)


async def test_undecodable_file_replies_and_moves_on(settings):
    """A decode failure is permanent — reply and advance, never defer."""
    result = await handle_document(_msg("memo.txt", b"\x80\x81\xfd\xfe\xff\x00\x01"), settings)

    assert result.saved_path is None
    assert "인코딩" in result.reply
    assert not list(settings.inbox_dir.iterdir())
    assert _downloads(settings) == ["memo.txt"]  # the owner still gets the file back


async def test_oversized_txt_is_refused_but_the_original_is_kept(settings):
    result = await handle_document(_msg("big.txt", b"x" * 2_000_001), settings)
    assert result.saved_path is None
    assert "너무 커서" in result.reply
    assert _downloads(settings) == ["big.txt"]


async def test_long_txt_is_capped_and_says_so(settings):
    result = await handle_document(_msg("long.txt", ("가" * 60_000).encode("utf-8")), settings)

    assert len(_body(result.saved_path).strip()) == 50_000
    assert "50,000자" in result.reply


async def test_empty_txt_writes_no_note(settings):
    result = await handle_document(_msg("empty.txt", b"   \n  "), settings)
    assert result.saved_path is None
    assert "비어" in result.reply


# =================================================================== .csv
CSV = "이름,수량\n사과,3\n배,5\n"


async def test_csv_becomes_a_markdown_table(settings):
    result = await handle_document(_msg("stock.csv", CSV.encode("utf-8")), settings)

    body = _body(result.saved_path)
    assert "| 이름 | 수량 |" in body
    assert "| --- | --- |" in body
    assert "| 사과 | 3 |" in body


async def test_csv_table_is_rendered_not_transcribed(llm_settings):
    """The LLM must never retype the owner's numbers — a silently altered figure in a data file
    is precisely the error nothing downstream can catch."""
    engine = _metadata({"title": "재고", "tags": [], "summary": "재고 현황."})
    result = await handle_document(
        _msg("stock.csv", CSV.encode("utf-8")), llm_settings, engine=engine
    )

    body = _body(result.saved_path)
    assert "| 사과 | 3 |" in body  # deterministic render, byte-identical to the file
    assert _fm(result.saved_path)["title"] == "재고"  # the model supplied only this


async def test_csv_in_cp949_from_excel(settings):
    result = await handle_document(_msg("stock.csv", CSV.encode("cp949")), settings)
    assert "| 사과 | 3 |" in _body(result.saved_path)


async def test_csv_row_cap_is_reported(settings):
    rows = "\n".join(f"항목{i},{i}" for i in range(300))
    result = await handle_document(_msg("big.csv", f"a,b\n{rows}\n".encode("utf-8")), settings)

    body = _body(result.saved_path)
    assert "| 항목199 | 199 |" in body
    assert "| 항목200 | 200 |" not in body
    assert "300행 중 200행" in body  # the note itself says it is a preview
    assert "300행 중 200행" in result.reply


async def test_empty_csv_writes_no_note(settings):
    result = await handle_document(_msg("empty.csv", b""), settings)
    assert result.saved_path is None
    assert _downloads(settings) == ["empty.csv"]


# =================================================================== .pdf
MARKDOWN = "# 분기 보고서\n\n## 요약\n\n매출이 증가했습니다.\n"


async def test_pdf_converts_to_a_note(llm_settings):
    engine = FakeEngine(text=MARKDOWN)
    result = await handle_document(
        _msg("report.pdf", b"%PDF-1.4 fake"), llm_settings, engine=engine
    )

    assert result.saved_path is not None
    assert "매출이 증가했습니다." in _body(result.saved_path)
    fm = _fm(result.saved_path)
    assert fm["title"] == "분기 보고서"  # the document's own heading
    assert fm["type"] == "document"
    assert fm["original_file"] == "report.pdf"


async def test_pdf_title_falls_back_to_the_filename(llm_settings):
    engine = FakeEngine(text="본문만 있고 제목 헤딩이 없는 문서입니다.")
    result = await handle_document(_msg("무제 문서.pdf", b"%PDF"), llm_settings, engine=engine)
    assert _fm(result.saved_path)["title"] == "무제 문서"


async def test_pdf_original_moves_to_downloads(llm_settings):
    engine = FakeEngine(text=MARKDOWN)
    await handle_document(_msg("report.pdf", b"%PDF"), llm_settings, engine=engine)
    assert _downloads(llm_settings) == ["report.pdf"]


async def test_pdf_is_staged_alone_in_an_isolated_dir(llm_settings):
    """The model fabricates from a neighbour rather than admitting it cannot read its input, so
    the staging dir must contain the PDF and nothing else — and be the job's cwd."""
    engine = FakeEngine(text=MARKDOWN)
    await handle_document(_msg("report.pdf", b"%PDF"), llm_settings, engine=engine)

    call = engine.calls[0]
    stage = call["cwd"]
    assert call["visible"] == ["report.pdf"]  # no neighbours to fabricate from
    assert list(call["add_dirs"]) == [stage]  # nothing else is readable
    assert str(stage / "report.pdf") in call["prompt"]  # pointed at the staged copy


async def test_pdf_prompt_carries_the_path_and_the_sentinel(llm_settings):
    engine = FakeEngine(text=MARKDOWN)
    await handle_document(_msg("report.pdf", b"%PDF"), llm_settings, engine=engine)

    prompt = engine.calls[0]["prompt"]
    assert "report.pdf" in prompt
    assert "CONVERSION_FAILED" in prompt


async def test_pdf_timeout_budget_clears_the_text_path_cap(llm_settings):
    """`Read` pages a PDF at ≤20 pages a call, so a long deck is several turns."""
    engine = FakeEngine(text=MARKDOWN)
    await handle_document(_msg("report.pdf", b"%PDF"), llm_settings, engine=engine)
    assert engine.calls[0]["timeout_sec"] >= 300


async def test_pdf_timeout_budget_honours_a_higher_setting(llm_settings):
    engine = FakeEngine(text=MARKDOWN)
    await handle_document(
        _msg("report.pdf", b"%PDF"), replace(llm_settings, claude_timeout_sec=900.0), engine=engine
    )
    assert engine.calls[0]["timeout_sec"] == 900.0


async def test_pdf_runs_on_the_pdf_model(llm_settings):
    """Conversion is flaky on the cheap default, so the PDF path uses claude_pdf_model (opus)."""
    engine = FakeEngine(text=MARKDOWN)
    settings = replace(llm_settings, claude_model="sonnet", claude_pdf_model="opus")
    await handle_document(_msg("report.pdf", b"%PDF"), settings, engine=engine)
    assert engine.calls[0]["model"] == "opus"


async def test_pdf_retries_a_flaky_sentinel_then_succeeds(llm_settings):
    """A readable file that spuriously returns the sentinel is retried, not permanently skipped."""
    engine = FakeEngine(texts=["CONVERSION_FAILED", MARKDOWN])
    result = await handle_document(_msg("report.pdf", b"%PDF"), llm_settings, engine=engine)

    assert len(engine.calls) == 2  # bailed once, converted on the retry
    assert result.saved_path is not None
    assert "매출이 증가했습니다." in _body(result.saved_path)


async def test_pdf_gives_up_after_max_attempts(llm_settings):
    """A genuinely unreadable file returns the sentinel every time and ends in a reported failure —
    the retry never turns a bad file into a fabricated note."""
    engine = FakeEngine(text="CONVERSION_FAILED")
    result = await handle_document(_msg("report.pdf", b"%PDF"), llm_settings, engine=engine)

    assert len(engine.calls) == 3  # _PDF_MAX_ATTEMPTS
    assert result.saved_path is None
    assert "변환하지 못했습니다" in result.reply
    assert _downloads(llm_settings) == ["report.pdf"]


async def test_pdf_truncated_stub_is_treated_as_failure(llm_settings):
    """A too-short reply slips past the sentinel check but is a hollow note — measured at 19 chars
    on a flaky run — so it must be retried, then reported if it never recovers."""
    engine = FakeEngine(text="# 제안서")  # under _PDF_MIN_OUTPUT_CHARS
    result = await handle_document(_msg("report.pdf", b"%PDF"), llm_settings, engine=engine)

    assert len(engine.calls) == 3
    assert result.saved_path is None
    assert "변환하지 못했습니다" in result.reply


async def test_pdf_short_stub_recovers_on_retry(llm_settings):
    engine = FakeEngine(texts=["# 짧음", MARKDOWN])
    result = await handle_document(_msg("report.pdf", b"%PDF"), llm_settings, engine=engine)
    assert result.saved_path is not None


async def test_sentinel_writes_no_note(llm_settings):
    """`is_error: False` is not proof of provenance — the model reports success on a fabrication,
    so the sentinel is what stands between us and a note built from the wrong source."""
    engine = FakeEngine(text="CONVERSION_FAILED")
    result = await handle_document(_msg("report.pdf", b"%PDF"), llm_settings, engine=engine)

    assert result.saved_path is None
    assert not list(llm_settings.inbox_dir.iterdir())
    assert "변환하지 못했습니다" in result.reply
    assert _downloads(llm_settings) == ["report.pdf"]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("CONVERSION_FAILED", id="bare"),
        pytest.param("CONVERSION_FAILED\n", id="trailing-newline"),
        pytest.param("`CONVERSION_FAILED`", id="backticked"),
        pytest.param("# CONVERSION_FAILED", id="as-a-heading"),
        pytest.param("CONVERSION_FAILED: the file could not be read", id="with-an-excuse"),
        pytest.param("", id="empty-output"),
        pytest.param("   \n  ", id="blank-output"),
    ],
)
async def test_sentinel_variants_are_all_caught(llm_settings, text):
    engine = FakeEngine(text=text)
    result = await handle_document(_msg("report.pdf", b"%PDF"), llm_settings, engine=engine)
    assert result.saved_path is None


async def test_sentinel_word_inside_a_real_document_is_not_a_false_positive(llm_settings):
    engine = FakeEngine(text="# 오류 코드 목록\n\n- CONVERSION_FAILED: 변환 실패 시 반환됩니다.\n")
    result = await handle_document(_msg("codes.pdf", b"%PDF"), llm_settings, engine=engine)
    assert result.saved_path is not None


@pytest.mark.parametrize(
    "engine",
    [
        pytest.param(FakeEngine(raises=ClaudeUnavailable("no cli")), id="cli-missing"),
        pytest.param(FakeEngine(raises=ClaudeTimeout("too slow")), id="timeout"),
    ],
)
async def test_pdf_engine_failure_writes_no_note_and_says_why(llm_settings, engine):
    """There is no no-LLM fallback here: rendering the page *is* the LLM's job. Don't write a
    worse note — write none, and tell the owner."""
    result = await handle_document(_msg("report.pdf", b"%PDF"), llm_settings, engine=engine)

    assert result.saved_path is None
    assert not list(llm_settings.inbox_dir.iterdir())
    assert "변환하지 못했습니다" in result.reply
    assert _downloads(llm_settings) == ["report.pdf"]


async def test_pdf_usage_limit_defers(llm_settings):
    engine = FakeEngine(raises=ClaudeUsageLimit("API Error: Claude AI usage limit reached"))
    with pytest.raises(DeferMessage, match="usage limit reached"):
        await handle_document(_msg("report.pdf", b"%PDF"), llm_settings, engine=engine)


async def test_pdf_deferral_leaves_no_side_effect(llm_settings):
    """The replay re-runs the handler from scratch, so a deferral must not write *or move*
    anything — a moved original would be gone by the time the replay looked for it."""
    engine = FakeEngine(raises=ClaudeUsageLimit("limit"))
    with pytest.raises(DeferMessage):
        await handle_document(_msg("report.pdf", b"%PDF"), llm_settings, engine=engine)

    assert not list(llm_settings.inbox_dir.iterdir())
    assert _downloads(llm_settings) == []


async def test_txt_usage_limit_defers_without_side_effects(llm_settings):
    engine = FakeEngine(raises=ClaudeUsageLimit("limit"))
    with pytest.raises(DeferMessage):
        await handle_document(_msg("memo.txt", b"body"), llm_settings, engine=engine)

    assert not list(llm_settings.inbox_dir.iterdir())
    assert _downloads(llm_settings) == []


async def test_txt_non_limit_failure_degrades_like_text(llm_settings):
    """Unlike a PDF, a .txt has a real no-LLM path: the body is already the note."""
    engine = FakeEngine(raises=ClaudeUnavailable("no cli"))
    result = await handle_document(
        _msg("memo.txt", "본문".encode("utf-8")), llm_settings, engine=engine
    )

    assert result.saved_path is not None
    assert "본문" in _body(result.saved_path)
    assert _fm(result.saved_path)["title"] == "memo"
    assert "보강에 실패" in result.reply


async def test_pdf_without_the_engine_says_so(settings):
    result = await handle_document(_msg("report.pdf", b"%PDF"), settings)
    assert result.saved_path is None
    assert "CLAUDE_ENABLED" in result.reply
    assert _downloads(settings) == ["report.pdf"]


# =================================================================== unsupported
@pytest.mark.parametrize("name", ["deck.pptx", "report.docx", "sheet.xlsx", "old.doc", "a.hwp"])
async def test_unsupported_formats_ask_for_a_pdf_export(settings, name):
    message = _msg(name, b"data")
    result = await handle_document(message, settings)

    assert result.saved_path is None
    assert "PDF로 내보내서" in result.reply
    assert not list(settings.inbox_dir.iterdir())


async def test_unsupported_format_is_not_even_downloaded(settings):
    message = _msg("report.docx", b"data")
    await handle_document(message, settings)
    assert message.raw.downloads == []


async def test_unsupported_format_is_treated_as_processed(settings):
    """A normal result (not an exception, not a deferral) is what advances the HWM — which is what
    stops the same .docx re-nagging on every Start."""
    result = await handle_document(_msg("report.docx", b"data"), settings)
    assert result.reply and result.saved_path is None


# =================================================================== download safety
async def test_a_traversing_filename_cannot_escape_the_temp_dir(settings):
    """A forwarded file's name is not the owner's own writing, so it is untrusted input."""
    message = _msg("../../evil.md", b"# x")
    result = await handle_document(message, settings)

    assert result.saved_path.parent == settings.inbox_dir
    assert Path(message.raw.downloads[0]).name == "evil.md"


async def test_a_message_without_an_attachment_is_a_fault(settings):
    message = _msg("memo.txt", b"")
    message.raw = None
    with pytest.raises(RuntimeError):
        await handle_document(message, settings)


# ------------------------------------------- the vault's filename category slot
async def test_a_pdf_is_filed_under_the_category_the_model_read(settings, tmp_path):
    """A file *is* a document, and only the model that read it can say what kind."""
    engine = FakeEngine(text="분류: 보고\n\n# 3분기 실적 보고서\n\n실적은 목표를 넘었다.")
    live = replace(settings, claude_enabled=True)

    result = await handle_document(_msg("report.pdf", b"%PDF-1.4"), live, engine=engine)

    assert result.saved_path.name == "260715-보고-3분기_실적_보고서.md"


async def test_the_category_line_never_reaches_the_note_body(settings, tmp_path):
    """It is addressed to a program. Left in, it is the document's first line — the same shape as
    the 태그: line increment 5 had to strip out of meeting notes."""
    engine = FakeEngine(text="분류: 전략\n\n# 방향성\n\n본문입니다.")
    live = replace(settings, claude_enabled=True)

    result = await handle_document(_msg("plan.pdf", b"%PDF-1.4"), live, engine=engine)

    body = result.saved_path.read_text(encoding="utf-8")
    assert "분류:" not in body
    assert body.rstrip().endswith("본문입니다.")


async def test_a_conversion_without_a_category_line_still_becomes_a_note(settings):
    """A forgetful model has not failed — never cost the owner a note it did produce."""
    engine = FakeEngine(text="# 제목입니다\n\n본문이 충분히 깁니다.")
    live = replace(settings, claude_enabled=True)

    result = await handle_document(_msg("x.pdf", b"%PDF-1.4"), live, engine=engine)

    assert result.saved_path.name == "260715-노트-제목입니다.md"


async def test_an_invented_category_falls_back_rather_than_naming_the_file(settings):
    engine = FakeEngine(text="분류: 전략기획보고서\n\n# 제목\n\n본문이 충분히 깁니다.")
    live = replace(settings, claude_enabled=True)

    result = await handle_document(_msg("x.pdf", b"%PDF-1.4"), live, engine=engine)

    assert result.saved_path.name.startswith("260715-노트-")


async def test_a_sent_markdown_file_is_a_note(settings):
    """This route never calls the engine, so nobody read the document to classify it."""
    result = await handle_document(_msg("idea.md", "# 아이디어".encode()), settings)

    assert result.saved_path.name == "260715-노트-idea.md"
