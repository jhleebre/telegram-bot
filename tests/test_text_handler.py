import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest
import yaml

from contextbot.engine.claude_cli import ClaudeResult, ClaudeTimeout, ClaudeUnavailable
from contextbot.handlers.base import IncomingMessage, MessageKind
from contextbot.handlers.text_handler import handle_text


def _msg(text: str) -> IncomingMessage:
    return IncomingMessage(
        user_id=42,
        chat_id=999,
        message_id=7,
        date=datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc),
        kind=MessageKind.TEXT,
        text=text,
    )


def _fm(path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---\n")[1])


class FakeEngine:
    """Stands in for ClaudeCLI: returns canned text or raises."""

    def __init__(self, *, text: str = "", raises: Exception | None = None):
        self._text = text
        self._raises = raises
        self.prompts: list[str] = []

    async def run(self, prompt: str, **kwargs):
        self.prompts.append(prompt)
        if self._raises is not None:
            raise self._raises
        return ClaudeResult(text=self._text, session_id="s-1")


def _replying(payload: dict) -> FakeEngine:
    return FakeEngine(text=json.dumps(payload, ensure_ascii=False))


@pytest.fixture
def llm_settings(settings):
    """Settings with the Phase 2 enrichment pass switched on."""
    return replace(settings, claude_enabled=True)


# --------------------------------------------------- Phase 1 behaviour (no LLM)
async def test_handle_text_writes_note(settings):
    result = await handle_text(_msg("첫 줄 제목\n본문 내용"), settings)
    assert result.saved_path is not None
    assert result.saved_path.exists()
    assert "저장됨" in result.reply

    content = result.saved_path.read_text(encoding="utf-8")
    fm = yaml.safe_load(content.split("---\n")[1])
    assert fm["title"] == "첫 줄 제목"
    assert fm["telegram_message_id"] == 7
    assert "본문 내용" in content


async def test_handle_empty_text(settings):
    result = await handle_text(_msg("   "), settings)
    assert result.saved_path is None
    assert not list(settings.inbox_dir.iterdir())


async def test_long_title_truncated(settings):
    long_line = "가" * 200
    result = await handle_text(_msg(long_line), settings)
    fm = _fm(result.saved_path)
    assert fm["title"].endswith("…")
    assert len(fm["title"]) <= 82


async def test_engine_not_called_when_disabled(settings):
    engine = FakeEngine(text='{"title": "should not be used"}')
    result = await handle_text(_msg("메모"), settings, engine=engine)

    assert engine.prompts == []
    assert _fm(result.saved_path)["title"] == "메모"


# --------------------------------------------------- Phase 2 enrichment
async def test_enrichment_populates_frontmatter(llm_settings):
    engine = _replying(
        {"title": "3분기 예산 검토", "tags": ["budget", "Q3"], "summary": "예산 회의 준비 메모."}
    )
    result = await handle_text(_msg("예산 회의 준비\n숫자 다시 확인"), llm_settings, engine=engine)

    fm = _fm(result.saved_path)
    assert fm["title"] == "3분기 예산 검토"
    assert fm["tags"] == ["budget", "q3"]
    assert fm["summary"] == "예산 회의 준비 메모."
    assert fm["telegram_message_id"] == 7
    # The body is always the original text, never the model's rewrite.
    assert "숫자 다시 확인" in result.saved_path.read_text(encoding="utf-8")


async def test_enriched_title_drives_filename(llm_settings):
    engine = _replying({"title": "budget review", "tags": []})
    result = await handle_text(_msg("무슨 메모"), llm_settings, engine=engine)
    assert "budget_review" in result.saved_path.name


async def test_note_text_is_sent_in_prompt(llm_settings):
    engine = _replying({"title": "t", "tags": []})
    await handle_text(_msg("고유한내용12345"), llm_settings, engine=engine)
    assert "고유한내용12345" in engine.prompts[0]


async def test_tags_normalized_and_capped(llm_settings):
    engine = _replying(
        {
            "title": "t",
            "tags": ["#Alpha", "beta gamma", "Alpha", "d", "e", "f", "g", 42, ""],
        }
    )
    result = await handle_text(_msg("메모"), llm_settings, engine=engine)
    # lowercased, '#' stripped, spaces hyphenated, de-duplicated, non-strings dropped, capped at 5
    assert _fm(result.saved_path)["tags"] == ["alpha", "beta-gamma", "d", "e", "f"]


async def test_overlong_enriched_title_and_summary_truncated(llm_settings):
    engine = _replying({"title": "가" * 300, "tags": [], "summary": "나" * 500})
    result = await handle_text(_msg("메모"), llm_settings, engine=engine)

    fm = _fm(result.saved_path)
    assert len(fm["title"]) <= 82 and fm["title"].endswith("…")
    assert len(fm["summary"]) <= 202 and fm["summary"].endswith("…")


async def test_summary_omitted_when_absent(llm_settings):
    engine = _replying({"title": "t", "tags": ["a"]})
    result = await handle_text(_msg("메모"), llm_settings, engine=engine)
    assert "summary" not in _fm(result.saved_path)


# --------------------------------------------------- Fallback: never lose a note
@pytest.mark.parametrize(
    "engine",
    [
        pytest.param(FakeEngine(raises=ClaudeUnavailable("no cli")), id="cli-missing"),
        pytest.param(FakeEngine(raises=ClaudeTimeout("too slow")), id="timeout"),
        pytest.param(FakeEngine(text="I'm afraid I can't do that"), id="non-json"),
        pytest.param(FakeEngine(text='{"tags": ["a"]}'), id="no-title"),
        pytest.param(FakeEngine(text='{"title": "   ", "tags": []}'), id="blank-title"),
        pytest.param(FakeEngine(text='{"title": 42}'), id="non-string-title"),
        pytest.param(FakeEngine(raises=RuntimeError("unexpected")), id="unexpected-error"),
    ],
)
async def test_falls_back_to_first_line_on_any_failure(llm_settings, engine):
    result = await handle_text(_msg("첫 줄 제목\n본문"), llm_settings, engine=engine)

    assert result.saved_path is not None and result.saved_path.exists()
    fm = _fm(result.saved_path)
    assert fm["title"] == "첫 줄 제목"
    assert fm["tags"] == []
    assert "summary" not in fm
    assert "본문" in result.saved_path.read_text(encoding="utf-8")
