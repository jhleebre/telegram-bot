import pytest

from contextbot.engine import prompts


def test_render_substitutes_text():
    out = prompts.render("text_enrich", text="회의 준비하기")
    assert "회의 준비하기" in out
    assert "{text}" not in out


def test_render_keeps_literal_json_braces():
    """The JSON shape example must survive str.format (doubled braces in the template)."""
    out = prompts.render("text_enrich", text="x")
    assert '{"title": "...", "tags": ["...", "..."], "summary": "..."}' in out


def test_unknown_template_raises():
    with pytest.raises(prompts.PromptNotFound):
        prompts.load("no_such_template")


def test_load_is_cached():
    assert prompts.load("text_enrich") is prompts.load("text_enrich")
