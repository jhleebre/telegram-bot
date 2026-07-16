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


def test_image_template_carries_the_path_and_sentinel():
    out = prompts.render("image_describe", path="/tmp/stage/shot.png", sentinel="DESCRIPTION_FAILED")
    assert "/tmp/stage/shot.png" in out
    assert "DESCRIPTION_FAILED" in out
    assert "{" not in out and "}" not in out


def _unwrapped(name: str) -> str:
    """The template with its line wrapping collapsed, so assertions can quote whole sentences."""
    return " ".join(prompts.render(name, **_values(name)).split())


def _values(name: str) -> dict:
    """Every placeholder each template takes, so one call can render any of them."""
    return {
        "path": "/x",
        "sentinel": "S",
        "text": "메모 본문",
        "questions_heading": "## 확인 요청",
    }


def test_image_template_forbids_describing_an_unseen_image():
    """The heic finding: Read returns raw bytes without erroring, and the model will happily
    describe the file header instead. files/images.py prevents it; this is the second line."""
    out = _unwrapped("image_describe")
    assert "never on the file's name, its bytes, or its metadata" in out
    assert "Never describe an image you were not actually shown" in out
    assert "If the Read tool hands you raw bytes rather than a rendered picture" in out


def test_review_draft_template_carries_the_memo_and_the_structure():
    out = prompts.render(
        "review_draft", text="인프라 예산 회의 메모", sentinel="DRAFT_FAILED",
        questions_heading="## 확인 요청",
    )
    assert "인프라 예산 회의 메모" in out
    assert "DRAFT_FAILED" in out
    assert "## 확인 요청" in out
    assert "{" not in out and "}" not in out


def test_review_revise_template_carries_the_reply_and_the_structure():
    out = prompts.render(
        "review_revise", text="담당자는 김철수", sentinel="DRAFT_FAILED",
        questions_heading="## 확인 요청",
    )
    assert "담당자는 김철수" in out
    assert "DRAFT_FAILED" in out
    assert "## 확인 요청" in out
    assert "{" not in out and "}" not in out


def test_review_draft_forbids_inventing_content():
    """The same trap in a new costume: increment 2's .docx → a neighbouring file, increment 3's
    .heic → the file header. A fragmentary memo is this route's version of "nothing to see"."""
    out = _unwrapped("review_draft")
    assert "Never invent content the memo does not contain" in out
    assert "the note is allowed to be short" in out


def test_review_draft_asks_about_content_not_wording():
    out = _unwrapped("review_draft")
    assert "Ask about the **content**, never about formatting" in out
    assert "do not manufacture questions to fill the list" in out


def test_review_revise_treats_the_reply_as_authoritative():
    out = _unwrapped("review_revise")
    assert "the author is right and the draft is wrong" in out


def test_review_revise_forbids_unannounced_rewrites():
    """The owner is reviewing a draft they have already read; silent edits elsewhere are how a
    review loses their trust — and they would never know to look."""
    out = _unwrapped("review_revise")
    assert "Keep everything the reply does not touch **exactly as it was**" in out
    assert "This is a revision, not a rewrite" in out


def test_review_revise_outputs_the_whole_note_not_a_diff():
    """The draft on disk is replaced wholesale by each turn's output, so a diff would truncate it."""
    out = _unwrapped("review_revise")
    assert "Output the whole thing every time, not a diff" in out


@pytest.mark.parametrize(
    "name", ["pdf_to_markdown", "image_describe", "review_draft", "review_revise"]
)
def test_body_templates_require_hyphens_for_ranges(name):
    """Every template that generates Markdown *body* text must carry the tilde rule.

    In GFM `~` is a strikethrough delimiter, so two ranges in one paragraph or table cell silently
    eat the text between them (`10~20명, 예산 5~6천만원` → `10<del>20명, 예산 5</del>6천만원`).
    A single tilde renders fine, which is exactly why this needs stating rather than discovering.
    These prompts also demand faithful transcription, so the rule has to be explicit that the
    range separator is the one character allowed to change.
    """
    out = _unwrapped(name)
    assert "Ranges use a hyphen, never a tilde" in out
    assert "10-20명" in out
    # …and that it must not be over-applied to a tilde that is not a range.
    assert "~/Projects" in out


def test_unknown_template_raises():
    with pytest.raises(prompts.PromptNotFound):
        prompts.load("no_such_template")


def test_load_is_cached():
    assert prompts.load("text_enrich") is prompts.load("text_enrich")
