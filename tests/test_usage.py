"""Reading the plan's remaining allowance out of `claude -p /usage`.

Every fixture below is real captured CLI output (v2.1.187), because the whole module is a parser
for someone else's prose — a hand-written sample that happens to match the regex would test nothing
except the regex against itself.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from contextbot.core.session_store import SessionStore
from contextbot.engine.claude_cli import ClaudeError, ClaudeUnavailable
from contextbot.engine.usage import (
    UsageReport,
    UsageWindow,
    format_reset,
    format_usage,
    parse_usage,
    usage_summary,
)
from contextbot.handlers.conversation import activate, bot_dm_status


@pytest.fixture
def store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "reviews")


@pytest.fixture
def review(store):
    """A drafted review the owner has been asked about."""
    pending = store.create(
        message_id=7,
        session_id="s",
        title="3분기 예산 회의",
        source_date=datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc),
    )
    pending.write_draft("초안")
    activate(pending, store)
    return pending


REAL_OUTPUT = """You are currently using your subscription to power your Claude Code usage

Current session: 27% used · resets Jul 20 at 2:50pm (Asia/Seoul)
Current week (all models): 5% used · resets Jul 24 at 8pm (Asia/Seoul)
Current week (Fable): 0% used

What's contributing to your limits usage?
Approximate, based on local sessions on this machine — does not include other devices or claude.ai. Behaviors are independent characteristics, not a breakdown.

Last 7d · 2247 requests · 104 sessions
  82% of your usage was at >150k context
  52% of your usage came from sessions active for 8+ hours
  Top skills: /meeting 1%, /code-review 1%, /claude-api 1%
"""


# ----------------------------------------------------------------------------- parsing
def test_both_limit_windows_are_read_with_their_reset_stamps():
    report = parse_usage(REAL_OUTPUT)

    assert report.session == UsageWindow(27, "Jul 20 at 2:50pm (Asia/Seoul)")
    assert report.week == UsageWindow(5, "Jul 24 at 8pm (Asia/Seoul)")


def test_the_per_model_week_is_told_apart_from_the_all_models_one():
    """Both lines start `Current week (`, and reading the model's as the 7-day limit would report
    a number that is not the limit the owner is near."""
    report = parse_usage(REAL_OUTPUT)

    assert report.model_name == "Fable"
    assert report.model_week == UsageWindow(0, None)
    assert report.week.percent == 5, "the all-models line is still the 7일 한도"


def test_the_contributing_breakdown_is_not_mistaken_for_a_limit():
    """`/usage` prints several percentages below the limits — machine-local, approximate, and
    reshaped between releases. Only the three named lines are limits."""
    report = parse_usage(REAL_OUTPUT)

    assert format_usage(report).count("%") == 2, format_usage(report)
    assert "82" not in format_usage(report)


def test_output_that_says_nothing_recognisable_yields_an_empty_report():
    """The CLI is free to reword its own report; that must read as 'could not tell', not as 0%."""
    assert not parse_usage("You are currently using your subscription\n")
    assert not parse_usage("")


def test_a_window_with_no_reset_clause_still_reports_its_percentage():
    report = parse_usage("Current session: 100% used")

    assert report.session == UsageWindow(100, None)


# ---------------------------------------------------------------------------- rendering
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Jul 20 at 2:50pm (Asia/Seoul)", "7월 20일 오후 2:50"),
        ("Jul 24 at 8pm (Asia/Seoul)", "7월 24일 오후 8:00"),  # the CLI drops :00 on the hour
        ("Jan 3 at 12:05am (Asia/Seoul)", "1월 3일 오전 12:05"),
        ("Jan 3 at 12:05pm (Asia/Seoul)", "1월 3일 오후 12:05"),
    ],
)
def test_reset_stamps_are_rendered_in_korean(raw, expected):
    assert format_reset(raw) == expected


def test_an_unfamiliar_reset_wording_reaches_the_owner_verbatim():
    """Informational text must degrade to the CLI's own words, never to nothing."""
    assert format_reset("in about 2 hours") == "in about 2 hours"


def test_the_block_names_both_limits_by_their_windows():
    text = format_usage(parse_usage(REAL_OUTPUT))

    assert "5시간 한도: 27% 사용" in text
    assert "7일 한도: 5% 사용" in text
    assert "7월 20일 오후 2:50 리셋" in text


def test_an_unused_per_model_week_is_left_out():
    """It sits at 0% for anyone not using that model — a permanent empty row in a glanced-at DM."""
    assert "Fable" not in format_usage(parse_usage(REAL_OUTPUT))


def test_a_per_model_week_that_has_started_is_shown():
    report = parse_usage(REAL_OUTPUT.replace("(Fable): 0%", "(Fable): 12%"))

    assert "7일 한도 (Fable): 12% 사용" in format_usage(report)


def test_the_usage_block_carries_no_markdown():
    """`Notifier.send` passes no parse_mode, so asterisks would arrive as asterisks."""
    text = format_usage(parse_usage(REAL_OUTPUT))

    assert "**" not in text and "_" not in text


def test_a_partly_understood_report_shows_what_it_understood():
    text = format_usage(UsageReport(session=UsageWindow(27, None)))

    assert "5시간 한도: 27% 사용" in text
    assert "7일" not in text


# ------------------------------------------------------------------------- the live read
class _Engine:
    def __init__(self, text=None, error=None):
        self._text, self._error = text, error

    async def usage_text(self, *, timeout_sec):
        if self._error:
            raise self._error
        return self._text


def _summary(monkeypatch, engine):
    monkeypatch.setattr("contextbot.engine.usage.build_engine", lambda settings: engine)
    return asyncio.run(usage_summary(object()))


def test_a_live_read_is_rendered(monkeypatch):
    assert "5시간 한도: 27% 사용" in _summary(monkeypatch, _Engine(REAL_OUTPUT))


def test_an_engine_failure_becomes_one_line_and_never_raises(monkeypatch):
    """The status reply is the deliverable; the usage block is a footnote on it. A CLI that is
    missing, logged out, or slow must cost the owner the footnote and nothing else."""
    text = _summary(monkeypatch, _Engine(error=ClaudeUnavailable("'claude' not found")))

    assert text.startswith("📊 사용량은 확인하지 못했습니다")
    assert "not found" in text


def test_unreadable_output_says_so_rather_than_reporting_zero(monkeypatch):
    text = _summary(monkeypatch, _Engine("Something else entirely"))

    assert "확인하지 못했습니다" in text
    assert "0%" not in text


def test_the_failure_note_is_shown_rather_than_swallowed(monkeypatch):
    """Silence would leave an owner near their limit and an owner with a broken CLI looking at the
    identical message."""
    assert _summary(monkeypatch, _Engine(error=ClaudeError("boom"))).strip()


# --------------------------------------------------------------- where it gets attached
def test_the_idle_status_carries_the_usage_block(store):
    text = bot_dm_status(store, usage=format_usage(parse_usage(REAL_OUTPUT)))

    assert "🤖 실행 중입니다" in text
    assert "Saved Messages" in text
    assert "5시간 한도: 27% 사용" in text


def test_the_idle_status_is_unchanged_when_usage_could_not_be_read(store):
    """Nothing to append is not a reason to leave a blank tail on the message."""
    assert bot_dm_status(store, usage=None) == bot_dm_status(store, usage="").rstrip()


def test_an_open_review_is_not_buried_under_a_limits_table(store, review):
    """While a review waits, the reply's job is to name the draft and the two words that answer it."""
    text = bot_dm_status(store, usage=format_usage(parse_usage(REAL_OUTPUT)))

    assert "사용량" not in text
    assert "확인" in text and "취소" in text
