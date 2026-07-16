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
