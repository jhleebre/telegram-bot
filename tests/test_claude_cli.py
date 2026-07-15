"""Tests for the `claude -p` engine wrapper.

These drive the real subprocess path against a fake `claude` script (see conftest's
``make_claude``) — no model is ever invoked.
"""

import asyncio
from pathlib import Path

import pytest

from contextbot.config import Settings
from contextbot.engine import claude_cli
from contextbot.engine.claude_cli import (
    ClaudeCLI,
    ClaudeError,
    ClaudeTimeout,
    ClaudeUnavailable,
    ClaudeUsageLimit,
    build_engine,
    resolve_executable,
)
from .conftest import SUCCESS_RESULT, claude_prints


def _cli(script, **kw) -> ClaudeCLI:
    return ClaudeCLI(executable=str(script), **kw)


async def test_run_parses_result(make_claude):
    script = make_claude(claude_prints(SUCCESS_RESULT))
    result = await _cli(script).run("hi")

    assert result.text == "pong"
    assert result.session_id == "11111111-2222-3333-4444-555555555555"
    assert result.cost_usd == pytest.approx(0.0123)
    assert result.duration_ms == 4700
    assert result.num_turns == 1
    assert result.raw["type"] == "result"


async def test_prompt_is_sent_on_stdin_not_argv(make_claude, claude_calls):
    """Long prompts must not ride in argv, where they could exceed ARG_MAX."""
    script = make_claude(claude_prints(SUCCESS_RESULT))
    prompt = "회의록 " * 5000

    await _cli(script).run(prompt)

    call = claude_calls()[0]
    assert call["stdin"] == prompt
    assert prompt not in call["argv"]


async def test_argv_contains_model_and_json_format(make_claude, claude_calls):
    script = make_claude(claude_prints(SUCCESS_RESULT))
    await _cli(script, model="opus").run("hi")

    argv = claude_calls()[0]["argv"]
    assert "-p" in argv
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--output-format") + 1] == "json"


async def test_system_prompt_and_add_dirs(make_claude, claude_calls, tmp_path):
    script = make_claude(claude_prints(SUCCESS_RESULT))
    a, b = tmp_path / "a", tmp_path / "b"

    await _cli(script).run("hi", system_prompt="be terse", add_dirs=[a, b])

    argv = claude_calls()[0]["argv"]
    assert argv[argv.index("--system-prompt") + 1] == "be terse"
    assert [argv[i + 1] for i, v in enumerate(argv) if v == "--add-dir"] == [str(a), str(b)]


def _prints_cwd() -> str:
    return f"import os\nprint(json.dumps({{**{SUCCESS_RESULT!r}, 'result': os.getcwd()}}))"


async def test_per_call_cwd_runs_the_job_there(make_claude, tmp_path):
    """A job that reads a staged file must run *in* that directory, not the app's own."""
    stage = tmp_path / "stage"
    stage.mkdir()
    script = make_claude(_prints_cwd())

    result = await _cli(script).run("hi", cwd=stage)

    assert Path(result.text).resolve() == stage.resolve()


async def test_per_call_cwd_overrides_the_engine_default(make_claude, tmp_path):
    stage, default = tmp_path / "stage", tmp_path / "default"
    stage.mkdir()
    default.mkdir()
    script = make_claude(_prints_cwd())

    result = await _cli(script, cwd=default).run("hi", cwd=stage)

    assert Path(result.text).resolve() == stage.resolve()


async def test_resume_passes_session_id(make_claude, claude_calls):
    script = make_claude(claude_prints(SUCCESS_RESULT))
    await _cli(script).resume_session("abc-123", "그 용어는 틀렸어")

    argv = claude_calls()[0]["argv"]
    assert argv[argv.index("--resume") + 1] == "abc-123"
    assert "--session-id" not in argv


async def test_session_id_pins_new_session(make_claude, claude_calls):
    script = make_claude(claude_prints(SUCCESS_RESULT))
    await _cli(script).run("hi", session_id="fixed-uuid")

    argv = claude_calls()[0]["argv"]
    assert argv[argv.index("--session-id") + 1] == "fixed-uuid"


async def test_nonzero_exit_raises_with_stderr(make_claude):
    script = make_claude("sys.stderr.write('boom: bad flag')\ncode = 2")
    with pytest.raises(ClaudeError, match="exited 2"):
        await _cli(script).run("hi")


# ------------------------------------------------- API errors
# Shapes below are copied from real CLI v2.1.187 runs against an injected 429 endpoint.
# Note `subtype` stays "success" even when is_error is true, and stderr is empty — the useful
# detail is only in the stdout JSON, which the CLI still prints while exiting non-zero.
RATE_LIMITED = {
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "api_error_status": 429,
    "result": (
        "API Error: Server is temporarily limiting requests (not your usage limit) · "
        "Number of request tokens has exceeded your per-5-minute rate limit"
    ),
    "duration_ms": 57,
    "num_turns": 1,
    "total_cost_usd": 0,
}


async def test_usage_limit_is_typed_and_keeps_the_message(make_claude):
    """A 429 must be diagnosable from the log: exit code 1 + empty stderr alone is not."""
    script = make_claude(claude_prints(RATE_LIMITED) + "\ncode = 1")

    with pytest.raises(ClaudeUsageLimit) as exc:
        await _cli(script).run("hi")

    assert "per-5-minute rate limit" in str(exc.value)
    assert isinstance(exc.value, ClaudeError)  # callers catching ClaudeError still degrade


async def test_subscription_usage_limit_also_typed(make_claude):
    payload = {**RATE_LIMITED, "result": "API Error: Claude AI usage limit reached"}
    script = make_claude(claude_prints(payload) + "\ncode = 1")

    with pytest.raises(ClaudeUsageLimit, match="usage limit reached"):
        await _cli(script).run("hi")


async def test_other_api_error_reports_status(make_claude):
    payload = {**RATE_LIMITED, "api_error_status": 500, "result": "API Error: Internal"}
    script = make_claude(claude_prints(payload) + "\ncode = 1")

    with pytest.raises(ClaudeError, match="API error 500") as exc:
        await _cli(script).run("hi")
    assert not isinstance(exc.value, ClaudeUsageLimit)


async def test_error_json_on_zero_exit_still_raises(make_claude):
    """is_error must be honoured even if the CLI ever exits 0 alongside it."""
    script = make_claude(claude_prints(RATE_LIMITED))
    with pytest.raises(ClaudeUsageLimit):
        await _cli(script).run("hi")


async def test_nonzero_exit_with_empty_stderr_and_no_json(make_claude):
    """The exit-code path still reports something when there is no JSON to explain it."""
    script = make_claude("code = 1")
    with pytest.raises(ClaudeError, match="exited 1"):
        await _cli(script).run("hi")


async def test_non_json_output_raises(make_claude):
    script = make_claude("print('not json at all')")
    with pytest.raises(ClaudeError, match="non-JSON"):
        await _cli(script).run("hi")


async def test_empty_output_raises(make_claude):
    script = make_claude("pass")
    with pytest.raises(ClaudeError, match="no output"):
        await _cli(script).run("hi")


async def test_error_result_raises(make_claude):
    payload = {**SUCCESS_RESULT, "is_error": True, "subtype": "error_max_turns", "result": "nope"}
    script = make_claude(claude_prints(payload))
    with pytest.raises(ClaudeError, match="error_max_turns"):
        await _cli(script).run("hi")


async def test_timeout_raises_and_kills_child(make_claude, tmp_path):
    """A timed-out job must not leave the child running past the deadline."""
    survived = tmp_path / "survived.txt"
    script = make_claude(f"time.sleep(1)\nopen({str(survived)!r}, 'w').write('alive')")

    with pytest.raises(ClaudeTimeout, match="exceeded"):
        await _cli(script, timeout_sec=0.2).run("hi")

    # Wait past the child's own sleep: if it had outlived the kill, the marker would appear.
    await asyncio.sleep(1.5)
    assert not survived.exists()


async def test_per_call_timeout_overrides_default(make_claude):
    script = make_claude("time.sleep(30)")
    with pytest.raises(ClaudeTimeout):
        await _cli(script, timeout_sec=600).run("hi", timeout_sec=0.5)


async def test_missing_executable_raises_unavailable():
    cli = ClaudeCLI(executable="claude-does-not-exist-xyz")
    assert not cli.is_available()
    with pytest.raises(ClaudeUnavailable, match="CLAUDE_BIN"):
        await cli.run("hi")


async def test_available_for_real_script(make_claude):
    assert _cli(make_claude("pass")).is_available()


# ------------------------------------------------- executable resolution
# A Dock/Finder-launched GUI app inherits launchd's minimal PATH, so a Homebrew-installed
# `claude` that works in the terminal is invisible to a bare shutil.which lookup.
def test_resolve_prefers_path(make_claude, monkeypatch, tmp_path):
    script = make_claude("pass", name="claude")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert resolve_executable("claude") == str(script)


def test_resolve_falls_back_to_known_install_dir(make_claude, monkeypatch, tmp_path):
    """With `claude` off PATH but in a standard install dir, it is still found."""
    script = make_claude("pass", name="claude")
    monkeypatch.setenv("PATH", "/nonexistent-bin")
    monkeypatch.setattr(claude_cli, "_FALLBACK_BIN_DIRS", (str(tmp_path),))
    assert resolve_executable("claude") == str(script)


def test_resolve_returns_none_when_absent(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent-bin")
    monkeypatch.setattr(claude_cli, "_FALLBACK_BIN_DIRS", ("/nonexistent-bin",))
    assert resolve_executable("claude") is None


def test_resolve_explicit_path(make_claude, monkeypatch):
    script = make_claude("pass")
    monkeypatch.setenv("PATH", "/nonexistent-bin")
    assert resolve_executable(str(script)) == str(script)


def test_resolve_explicit_path_that_is_missing(tmp_path):
    assert resolve_executable(str(tmp_path / "nope" / "claude")) is None


def test_resolve_rejects_non_executable_file(tmp_path, monkeypatch):
    plain = tmp_path / "claude"
    plain.write_text("not executable")
    monkeypatch.setenv("PATH", "/nonexistent-bin")
    monkeypatch.setattr(claude_cli, "_FALLBACK_BIN_DIRS", (str(tmp_path),))
    assert resolve_executable("claude") is None


def test_resolve_rejects_directory(tmp_path, monkeypatch):
    """A *directory* named `claude` in an install dir must not be mistaken for the binary."""
    (tmp_path / "claude").mkdir()
    monkeypatch.setenv("PATH", "/nonexistent-bin")
    monkeypatch.setattr(claude_cli, "_FALLBACK_BIN_DIRS", (str(tmp_path),))
    assert resolve_executable("claude") is None


async def test_run_uses_resolved_path_off_path(make_claude, claude_calls, monkeypatch, tmp_path):
    """End-to-end: an engine configured with a bare name still runs a CLI that is off PATH."""
    make_claude(claude_prints(SUCCESS_RESULT), name="claude")
    monkeypatch.setenv("PATH", "/nonexistent-bin")
    monkeypatch.setattr(claude_cli, "_FALLBACK_BIN_DIRS", (str(tmp_path),))

    result = await ClaudeCLI(executable="claude").run("hi")
    assert result.text == "pong"
    assert len(claude_calls()) == 1


def test_build_engine_from_settings(tmp_path):
    settings = Settings(
        api_id=1,
        api_hash="x",
        session_path=tmp_path / "s",
        telegram_bot_token="t",
        inbox_dir=tmp_path,
        claude_bin="/opt/claude",
        claude_model="haiku",
        claude_timeout_sec=42.0,
    )
    engine = build_engine(settings)
    assert (engine.executable, engine.model, engine.timeout_sec) == ("/opt/claude", "haiku", 42.0)
