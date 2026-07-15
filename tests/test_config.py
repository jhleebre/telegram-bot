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
