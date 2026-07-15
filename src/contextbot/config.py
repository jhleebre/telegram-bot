"""Application configuration loaded from the environment / a `.env` file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # optional dependency; the loader also works from a pre-populated environment
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is a declared dependency
    load_dotenv = None  # type: ignore[assignment]

DEFAULT_INBOX_DIR = "~/Documents/MarkNotes/0_inbox"
# Project root is two levels up from this file (src/contextbot/config.py).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SESSION_PATH = _PROJECT_ROOT / "state" / "contextbot.session"


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


def _expand(path: str) -> Path:
    return Path(path).expanduser()


@dataclass(frozen=True)
class Settings:
    """Validated runtime settings.

    Secrets (``api_hash``, ``telegram_bot_token``) are excluded from ``__repr__`` so they never
    leak into logs.

    - ``api_id`` / ``api_hash`` / ``session_path``: Telethon *user* client — reads Saved Messages.
    - ``telegram_bot_token``: Bot API client — send-only replies to the owner's bot DM.
    - ``owner_chat_id``: optional override for the reply target; normally derived from the user's
      own account id at runtime.
    """

    api_id: int
    api_hash: str
    session_path: Path
    telegram_bot_token: str
    inbox_dir: Path
    owner_chat_id: int | None = None
    log_level: str = "INFO"

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"Settings(api_id={self.api_id}, api_hash=<hidden>, "
            f"session_path={self.session_path!s}, telegram_bot_token=<hidden>, "
            f"inbox_dir={self.inbox_dir!s}, owner_chat_id={self.owner_chat_id}, "
            f"log_level={self.log_level!r})"
        )

    @classmethod
    def load(
        cls,
        env: dict[str, str] | None = None,
        *,
        use_dotenv: bool = True,
        dotenv_path: str | os.PathLike[str] | None = None,
    ) -> "Settings":
        """Build :class:`Settings` from a mapping (defaults to ``os.environ``).

        Raises :class:`ConfigError` with an aggregated, human-readable message when
        anything required is missing or malformed.
        """
        if env is None:
            if use_dotenv and load_dotenv is not None:
                load_dotenv(dotenv_path=dotenv_path)
            env = dict(os.environ)

        errors: list[str] = []

        raw_api_id = (env.get("TELEGRAM_API_ID") or "").strip()
        api_id = 0
        if not raw_api_id:
            errors.append("TELEGRAM_API_ID is missing or empty (get it at https://my.telegram.org)")
        else:
            try:
                api_id = int(raw_api_id)
            except ValueError:
                errors.append(f"TELEGRAM_API_ID must be an integer, got {raw_api_id!r}")

        api_hash = (env.get("TELEGRAM_API_HASH") or "").strip()
        if not api_hash:
            errors.append("TELEGRAM_API_HASH is missing or empty (get it at https://my.telegram.org)")

        token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            errors.append("TELEGRAM_BOT_TOKEN is missing or empty (create a bot via @BotFather)")

        session_path = _expand(env.get("TELEGRAM_SESSION") or str(DEFAULT_SESSION_PATH))

        owner_chat_id: int | None = None
        raw_owner = (env.get("OWNER_CHAT_ID") or "").strip()
        if raw_owner:
            try:
                owner_chat_id = int(raw_owner)
            except ValueError:
                errors.append(f"OWNER_CHAT_ID must be an integer, got {raw_owner!r}")

        inbox_dir = _expand(env.get("INBOX_DIR") or DEFAULT_INBOX_DIR)
        if not inbox_dir.parent.exists():
            errors.append(
                f"INBOX_DIR parent does not exist: {inbox_dir.parent} "
                "(create the knowledge-base folder first)"
            )

        log_level = (env.get("LOG_LEVEL") or "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            errors.append(f"LOG_LEVEL must be a valid level, got {log_level!r}")

        if errors:
            raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(errors))

        return cls(
            api_id=api_id,
            api_hash=api_hash,
            session_path=session_path,
            telegram_bot_token=token,
            inbox_dir=inbox_dir,
            owner_chat_id=owner_chat_id,
            log_level=log_level,
        )
