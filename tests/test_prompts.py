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


def test_pdf_template_carries_the_path_and_sentinel():
    out = prompts.render(
        "pdf_to_markdown", path="/tmp/stage/report.pdf", sentinel="CONVERSION_FAILED"
    )
    assert "/tmp/stage/report.pdf" in out
    assert "CONVERSION_FAILED" in out
    assert "{" not in out and "}" not in out


def test_pdf_template_forbids_substituting_another_source():
    """The fabrication guard is the prompt's job; the sentinel check is only the backstop."""
    out = prompts.render("pdf_to_markdown", path="/x.pdf", sentinel="S")
    assert "only file in its directory" in out
    assert "fall back to any other file" in out
    assert "never reconstruct the content from your own" in out


def test_unknown_template_raises():
    with pytest.raises(prompts.PromptNotFound):
        prompts.load("no_such_template")


def test_load_is_cached():
    assert prompts.load("text_enrich") is prompts.load("text_enrich")
