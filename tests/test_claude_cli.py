"""Tests for the `claude -p` engine wrapper.

These drive the real subprocess path against a fake `claude` script (see conftest's
``make_claude``) — no model is ever invoked.
"""

import asyncio

import pytest

from contextbot.config import Settings
from contextbot.engine.claude_cli import (
    ClaudeCLI,
    ClaudeError,
    ClaudeTimeout,
    ClaudeUnavailable,
    build_engine,
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
    with pytest.raises(ClaudeUnavailable):
        await cli.run("hi")


async def test_available_for_real_script(make_claude):
    assert _cli(make_claude("pass")).is_available()


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
