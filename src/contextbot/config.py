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
# Where originals (pdf/txt/csv) land once their note is written. See files/originals.py.
DEFAULT_DOWNLOADS_DIR = "~/Downloads"
# Project root is two levels up from this file (src/contextbot/config.py).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SESSION_PATH = _PROJECT_ROOT / "state" / "contextbot.session"

# Phase 2 engine defaults (`claude -p`). Model/timeout are env-tunable per pipeline needs.
DEFAULT_CLAUDE_BIN = "claude"
DEFAULT_CLAUDE_MODEL = "sonnet"
# PDF conversion reads a whole document (often image-heavy slides) and is measurably flaky on the
# cheap default — sonnet took the "cannot read" escape hatch on ~60% of runs of a real 8-page deck,
# where opus converted it every time. PDFs are occasional, so the heavier model is worth it here.
DEFAULT_CLAUDE_PDF_MODEL = "opus"
# Images stay on the cheap default — measured, not assumed. Describing a single screenshot is a
# far lighter job than converting a whole deck: sonnet described a Korean screenshot (table,
# numbers, mixed Korean/English) correctly on 5 of 5 runs, transcribing every figure, in 2 turns
# for ~$0.025 est. The PDF route's flakiness never appeared, so there is nothing to buy by
# defaulting to opus. CLAUDE_IMAGE_MODEL is the dial if a real photo ever proves harder.
DEFAULT_CLAUDE_IMAGE_MODEL = "sonnet"
DEFAULT_CLAUDE_TIMEOUT_SEC = 120.0

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


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
    - ``downloads_dir``: where document originals are moved after their note is written.
    - ``claude_*``: Phase 2 engine — the headless ``claude -p`` CLI. When ``claude_enabled`` is
      False (or the CLI is missing), pipelines fall back to their no-LLM path.
    """

    api_id: int
    api_hash: str
    session_path: Path
    telegram_bot_token: str
    inbox_dir: Path
    downloads_dir: Path = Path(DEFAULT_DOWNLOADS_DIR).expanduser()
    owner_chat_id: int | None = None
    log_level: str = "INFO"
    claude_enabled: bool = True
    claude_bin: str = DEFAULT_CLAUDE_BIN
    claude_model: str = DEFAULT_CLAUDE_MODEL
    claude_pdf_model: str = DEFAULT_CLAUDE_PDF_MODEL
    claude_image_model: str = DEFAULT_CLAUDE_IMAGE_MODEL
    claude_timeout_sec: float = DEFAULT_CLAUDE_TIMEOUT_SEC

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"Settings(api_id={self.api_id}, api_hash=<hidden>, "
            f"session_path={self.session_path!s}, telegram_bot_token=<hidden>, "
            f"inbox_dir={self.inbox_dir!s}, downloads_dir={self.downloads_dir!s}, "
            f"owner_chat_id={self.owner_chat_id}, "
            f"log_level={self.log_level!r}, claude_enabled={self.claude_enabled}, "
            f"claude_bin={self.claude_bin!r}, claude_model={self.claude_model!r}, "
            f"claude_pdf_model={self.claude_pdf_model!r}, "
            f"claude_image_model={self.claude_image_model!r}, "
            f"claude_timeout_sec={self.claude_timeout_sec})"
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

        # No existence check: unlike the inbox (whose parent must be the real knowledge base),
        # this is created on demand the first time an original is moved.
        downloads_dir = _expand(env.get("DOWNLOADS_DIR") or DEFAULT_DOWNLOADS_DIR)

        log_level = (env.get("LOG_LEVEL") or "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            errors.append(f"LOG_LEVEL must be a valid level, got {log_level!r}")

        claude_enabled = True
        raw_enabled = (env.get("CLAUDE_ENABLED") or "").strip().lower()
        if raw_enabled:
            if raw_enabled in _TRUE_VALUES:
                claude_enabled = True
            elif raw_enabled in _FALSE_VALUES:
                claude_enabled = False
            else:
                errors.append(f"CLAUDE_ENABLED must be true/false, got {raw_enabled!r}")

        claude_bin = (env.get("CLAUDE_BIN") or DEFAULT_CLAUDE_BIN).strip()
        claude_model = (env.get("CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL).strip()
        claude_pdf_model = (env.get("CLAUDE_PDF_MODEL") or DEFAULT_CLAUDE_PDF_MODEL).strip()
        claude_image_model = (env.get("CLAUDE_IMAGE_MODEL") or DEFAULT_CLAUDE_IMAGE_MODEL).strip()

        claude_timeout_sec = DEFAULT_CLAUDE_TIMEOUT_SEC
        raw_timeout = (env.get("CLAUDE_TIMEOUT_SEC") or "").strip()
        if raw_timeout:
            try:
                claude_timeout_sec = float(raw_timeout)
            except ValueError:
                errors.append(f"CLAUDE_TIMEOUT_SEC must be a number, got {raw_timeout!r}")
            else:
                if claude_timeout_sec <= 0:
                    errors.append(
                        f"CLAUDE_TIMEOUT_SEC must be positive, got {claude_timeout_sec}"
                    )

        if errors:
            raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(errors))

        return cls(
            api_id=api_id,
            api_hash=api_hash,
            session_path=session_path,
            telegram_bot_token=token,
            inbox_dir=inbox_dir,
            downloads_dir=downloads_dir,
            owner_chat_id=owner_chat_id,
            log_level=log_level,
            claude_enabled=claude_enabled,
            claude_bin=claude_bin,
            claude_model=claude_model,
            claude_pdf_model=claude_pdf_model,
            claude_image_model=claude_image_model,
            claude_timeout_sec=claude_timeout_sec,
        )
