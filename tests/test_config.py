from pathlib import Path

import pytest

from contextbot.config import ConfigError, Settings


def _base_env(tmp_path: Path) -> dict[str, str]:
    return {
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "deadbeefdeadbeefdeadbeefdeadbeef",
        "TELEGRAM_BOT_TOKEN": "123:ABC",
        "INBOX_DIR": str(tmp_path / "inbox"),
        "LOG_LEVEL": "INFO",
    }


def test_load_valid(tmp_path):
    settings = Settings.load(_base_env(tmp_path), use_dotenv=False)
    assert settings.api_id == 12345
    assert settings.api_hash == "deadbeefdeadbeefdeadbeefdeadbeef"
    assert settings.telegram_bot_token == "123:ABC"
    assert settings.inbox_dir == tmp_path / "inbox"
    assert settings.owner_chat_id is None
    assert settings.log_level == "INFO"


def test_downloads_dir_defaults_to_home(tmp_path):
    settings = Settings.load(_base_env(tmp_path), use_dotenv=False)
    assert settings.downloads_dir == Path("~/Downloads").expanduser()


def test_downloads_dir_override(tmp_path):
    env = _base_env(tmp_path) | {"DOWNLOADS_DIR": "~/Files/in"}
    assert Settings.load(env, use_dotenv=False).downloads_dir == Path("~/Files/in").expanduser()


def test_downloads_dir_need_not_exist_yet(tmp_path):
    """Unlike the inbox, it is created on demand — a missing dir is not a config error."""
    env = _base_env(tmp_path) | {"DOWNLOADS_DIR": str(tmp_path / "nope" / "Downloads")}
    assert Settings.load(env, use_dotenv=False).downloads_dir.name == "Downloads"


def test_default_session_path(tmp_path):
    settings = Settings.load(_base_env(tmp_path), use_dotenv=False)
    assert settings.session_path.name == "contextbot.session"


def test_owner_chat_id_override(tmp_path):
    env = _base_env(tmp_path)
    env["OWNER_CHAT_ID"] = "999"
    settings = Settings.load(env, use_dotenv=False)
    assert settings.owner_chat_id == 999


def test_missing_api_id(tmp_path):
    env = _base_env(tmp_path)
    del env["TELEGRAM_API_ID"]
    with pytest.raises(ConfigError) as exc:
        Settings.load(env, use_dotenv=False)
    assert "TELEGRAM_API_ID" in str(exc.value)


def test_non_integer_api_id(tmp_path):
    env = _base_env(tmp_path)
    env["TELEGRAM_API_ID"] = "abc"
    with pytest.raises(ConfigError) as exc:
        Settings.load(env, use_dotenv=False)
    assert "TELEGRAM_API_ID" in str(exc.value)


def test_missing_api_hash(tmp_path):
    env = _base_env(tmp_path)
    del env["TELEGRAM_API_HASH"]
    with pytest.raises(ConfigError) as exc:
        Settings.load(env, use_dotenv=False)
    assert "TELEGRAM_API_HASH" in str(exc.value)


def test_missing_bot_token(tmp_path):
    env = _base_env(tmp_path)
    del env["TELEGRAM_BOT_TOKEN"]
    with pytest.raises(ConfigError) as exc:
        Settings.load(env, use_dotenv=False)
    assert "TELEGRAM_BOT_TOKEN" in str(exc.value)


def test_inbox_parent_missing(tmp_path):
    env = _base_env(tmp_path)
    env["INBOX_DIR"] = str(tmp_path / "nope" / "inbox")
    with pytest.raises(ConfigError) as exc:
        Settings.load(env, use_dotenv=False)
    assert "INBOX_DIR" in str(exc.value)


def test_invalid_log_level(tmp_path):
    env = _base_env(tmp_path)
    env["LOG_LEVEL"] = "LOUD"
    with pytest.raises(ConfigError):
        Settings.load(env, use_dotenv=False)


def test_secrets_hidden_in_repr(tmp_path):
    settings = Settings.load(_base_env(tmp_path), use_dotenv=False)
    text = repr(settings)
    assert "deadbeef" not in text
    assert "123:ABC" not in text
    assert "hidden" in text


def test_aggregates_multiple_errors(tmp_path):
    with pytest.raises(ConfigError) as exc:
        Settings.load({}, use_dotenv=False)
    msg = str(exc.value)
    assert "TELEGRAM_API_ID" in msg
    assert "TELEGRAM_API_HASH" in msg
    assert "TELEGRAM_BOT_TOKEN" in msg


# ------------------------------------------------- Phase 2 engine settings
def test_claude_defaults(tmp_path):
    settings = Settings.load(_base_env(tmp_path), use_dotenv=False)
    assert settings.claude_enabled is True
    assert settings.claude_bin == "claude"
    assert settings.claude_model == "sonnet"
    # PDF conversion defaults to a stronger model than text enrichment (measured flaky on sonnet).
    assert settings.claude_pdf_model == "opus"
    # Images stay on the cheap default: measured 5/5 on sonnet, so opus buys nothing here.
    assert settings.claude_image_model == "sonnet"
    assert settings.claude_timeout_sec == 120.0


def test_claude_overrides(tmp_path):
    env = _base_env(tmp_path)
    env.update(
        CLAUDE_BIN="/opt/homebrew/bin/claude",
        CLAUDE_MODEL="opus",
        CLAUDE_PDF_MODEL="sonnet",
        CLAUDE_IMAGE_MODEL="opus",
        CLAUDE_TIMEOUT_SEC="300",
    )
    settings = Settings.load(env, use_dotenv=False)
    assert settings.claude_bin == "/opt/homebrew/bin/claude"
    assert settings.claude_model == "opus"
    assert settings.claude_pdf_model == "sonnet"
    assert settings.claude_image_model == "opus"
    assert settings.claude_timeout_sec == 300.0


# --------------------------------------------------------- increment 5: audio
def test_meeting_and_whisper_defaults(tmp_path):
    settings = Settings.load(_base_env(tmp_path), use_dotenv=False)
    assert settings.claude_meeting_model == "sonnet"
    assert settings.whisper_model == "mlx-community/whisper-large-v3-turbo"
    assert settings.whisper_language == "ko"


def test_meeting_and_whisper_overrides(tmp_path):
    env = _base_env(tmp_path)
    env.update(
        CLAUDE_MEETING_MODEL="opus",
        WHISPER_MODEL="mlx-community/whisper-tiny",
        WHISPER_LANGUAGE="en",
    )
    settings = Settings.load(env, use_dotenv=False)
    assert settings.claude_meeting_model == "opus"
    assert settings.whisper_model == "mlx-community/whisper-tiny"
    assert settings.whisper_language == "en"


def test_glossary_defaults_into_the_vault(tmp_path):
    """It lives in the vault, not this repo: private, and already backed up by the owner."""
    settings = Settings.load(_base_env(tmp_path), use_dotenv=False)
    assert settings.glossary_path == Path(
        "~/Documents/MarkNotes/.claude/contextbot/glossary.md"
    ).expanduser()


def test_glossary_path_override(tmp_path):
    env = _base_env(tmp_path)
    env["GLOSSARY_PATH"] = str(tmp_path / "terms.md")
    assert Settings.load(env, use_dotenv=False).glossary_path == tmp_path / "terms.md"


def test_a_missing_glossary_is_not_a_config_error(tmp_path):
    """Absent is a valid state — no substitutions, note still made. Not a reason to refuse to start.

    Unlike INBOX_DIR, whose parent must be the real knowledge base.
    """
    env = _base_env(tmp_path)
    env["GLOSSARY_PATH"] = str(tmp_path / "nowhere" / "terms.md")
    settings = Settings.load(env, use_dotenv=False)
    assert not settings.glossary_path.exists()




@pytest.mark.parametrize(
    "raw,expected",
    [("false", False), ("0", False), ("no", False), ("off", False),
     ("true", True), ("1", True), ("YES", True), ("On", True)],
)
def test_claude_enabled_parsing(tmp_path, raw, expected):
    env = _base_env(tmp_path)
    env["CLAUDE_ENABLED"] = raw
    assert Settings.load(env, use_dotenv=False).claude_enabled is expected


def test_invalid_claude_enabled(tmp_path):
    env = _base_env(tmp_path)
    env["CLAUDE_ENABLED"] = "maybe"
    with pytest.raises(ConfigError, match="CLAUDE_ENABLED"):
        Settings.load(env, use_dotenv=False)


@pytest.mark.parametrize("raw", ["abc", "0", "-5"])
def test_invalid_claude_timeout(tmp_path, raw):
    env = _base_env(tmp_path)
    env["CLAUDE_TIMEOUT_SEC"] = raw
    with pytest.raises(ConfigError, match="CLAUDE_TIMEOUT_SEC"):
        Settings.load(env, use_dotenv=False)


def test_review_expiry_defaults_to_the_bot_apis_retention_window(tmp_path):
    """24h is not a taste call: past it, Telegram drops a reply sent while the app was closed, so
    the review could never be finished anyway."""
    assert Settings.load(_base_env(tmp_path), use_dotenv=False).review_expiry_hours == 24.0


def test_review_expiry_override(tmp_path):
    env = _base_env(tmp_path) | {"REVIEW_EXPIRY_HOURS": "2.5"}
    assert Settings.load(env, use_dotenv=False).review_expiry_hours == 2.5


@pytest.mark.parametrize("raw", ["abc", "0", "-1"])
def test_invalid_review_expiry(tmp_path, raw):
    env = _base_env(tmp_path)
    env["REVIEW_EXPIRY_HOURS"] = raw
    with pytest.raises(ConfigError, match="REVIEW_EXPIRY_HOURS"):
        Settings.load(env, use_dotenv=False)


def test_the_meeting_timeout_is_a_hang_detector_not_a_budget(tmp_path):
    """Measured: a real ~hour-long meeting's draft turn took 423s. The owner's meetings run past an
    hour, and being too short does not delay — it *loses the note* (ClaudeTimeout degrades to a
    transcript-only note, throwing away the Whisper run and the drafting pass)."""
    settings = Settings.load(_base_env(tmp_path), use_dotenv=False)
    assert settings.claude_meeting_timeout_sec == 3600.0


def test_the_meeting_timeout_is_independent_of_the_shared_default(tmp_path):
    """A meeting's length has nothing to do with how long a text memo may take, so it is its own
    dial rather than a floor over CLAUDE_TIMEOUT_SEC."""
    env = _base_env(tmp_path)
    env.update(CLAUDE_TIMEOUT_SEC="120", CLAUDE_MEETING_TIMEOUT_SEC="7200")
    settings = Settings.load(env, use_dotenv=False)
    assert settings.claude_timeout_sec == 120.0
    assert settings.claude_meeting_timeout_sec == 7200.0


@pytest.mark.parametrize("bad", ["0", "-1", "abc"])
def test_a_bad_meeting_timeout_is_a_config_error(tmp_path, bad):
    env = _base_env(tmp_path)
    env["CLAUDE_MEETING_TIMEOUT_SEC"] = bad
    with pytest.raises(ConfigError, match="CLAUDE_MEETING_TIMEOUT_SEC"):
        Settings.load(env, use_dotenv=False)
