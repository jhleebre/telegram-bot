"""Shared fixtures and lightweight fakes for the test suite.

No real Telegram network calls are made anywhere; the Telethon client, the reply bot, and messages
are all duck-typed fakes.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from contextbot.config import Settings

OWNER_ID = 42


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    d = tmp_path / "0_inbox"
    d.mkdir()
    return d


@pytest.fixture
def settings(inbox: Path, tmp_path: Path) -> Settings:
    return Settings(
        api_id=12345,
        api_hash="deadbeefdeadbeefdeadbeefdeadbeef",
        session_path=tmp_path / "state" / "contextbot.session",
        telegram_bot_token="123:TEST",
        inbox_dir=inbox,
        owner_chat_id=None,
        log_level="INFO",
        # Off by default so handler tests exercise the no-LLM path unless they opt in.
        claude_enabled=False,
    )


# ------------------------------------------------------- `claude -p` engine fakes
SUCCESS_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "pong",
    "session_id": "11111111-2222-3333-4444-555555555555",
    "total_cost_usd": 0.0123,
    "duration_ms": 4700,
    "num_turns": 1,
}


@pytest.fixture
def make_claude(tmp_path: Path):
    """Factory for a fake `claude` executable.

    Returns a real script on disk, so tests drive ClaudeCLI's actual subprocess/stdin/argv path
    without ever invoking a model. The script records its argv and stdin to ``<script>.calls.json``.

    ``body`` is Python source appended to the script; it may print output and set ``code``
    (the exit status).
    """

    def _make(body: str, *, name: str = "claude") -> Path:
        script = tmp_path / name
        record = tmp_path / f"{name}.calls.json"
        script.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import json, sys, time
                stdin = sys.stdin.read()
                calls = []
                record = {str(record)!r}
                try:
                    with open(record) as fh:
                        calls = json.load(fh)
                except FileNotFoundError:
                    pass
                calls.append({{"argv": sys.argv[1:], "stdin": stdin}})
                with open(record, "w") as fh:
                    json.dump(calls, fh)
                code = 0
                """
            )
            + textwrap.dedent(body)
            + "\nsys.exit(code)\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return script

    return _make


@pytest.fixture
def claude_calls(tmp_path: Path):
    """Read back the invocations recorded by a `make_claude` script."""

    def _calls(name: str = "claude") -> list[dict]:
        record = tmp_path / f"{name}.calls.json"
        if not record.exists():
            return []
        return json.loads(record.read_text())

    return _calls


def claude_prints(payload: dict) -> str:
    """Script body that emits ``payload`` as the CLI's JSON result."""
    return f"print(json.dumps({payload!r}))"


# ------------------------------------------------------------- Telethon fakes
@dataclass
class FakeFile:
    name: Optional[str] = None
    mime_type: Optional[str] = None


@dataclass
class FakeMe:
    id: int = OWNER_ID
    username: str = "owner"


class FakeTMessage:
    """Stands in for a Telethon Message (duck-typed)."""

    def __init__(
        self,
        *,
        id: int,
        raw_text: str | None = None,
        date: datetime | None = None,
        voice=None,
        audio=None,
        photo=None,
        document=None,
        file: FakeFile | None = None,
        chat_id: int = OWNER_ID,
        sender_id: int = OWNER_ID,
    ):
        self.id = id
        self.raw_text = raw_text
        self.message = raw_text
        self.date = date or datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)
        self.voice = voice
        self.audio = audio
        self.photo = photo
        self.document = document
        self.file = file
        self.chat_id = chat_id
        self.sender_id = sender_id


def text_message(id: int, text: str, **kw) -> FakeTMessage:
    return FakeTMessage(id=id, raw_text=text, **kw)


def document_message(id: int, name: str, mime: str | None = None, **kw) -> FakeTMessage:
    return FakeTMessage(id=id, document=object(), file=FakeFile(name=name, mime_type=mime), **kw)


def voice_message(id: int, **kw) -> FakeTMessage:
    return FakeTMessage(id=id, voice=object(), file=FakeFile(mime_type="audio/ogg"), **kw)


def photo_message(id: int, **kw) -> FakeTMessage:
    return FakeTMessage(id=id, photo=object(), file=FakeFile(mime_type="image/jpeg"), **kw)


class FakeClient:
    """Stands in for telethon.TelegramClient."""

    def __init__(self, messages=None, *, authorized: bool = True, me_id: int = OWNER_ID):
        self._messages = list(messages or [])
        self._authorized = authorized
        self._connected = False
        self._me = FakeMe(id=me_id)
        self.handlers: list = []

    async def connect(self):
        self._connected = True

    async def disconnect(self):
        self._connected = False

    def is_connected(self):
        return self._connected

    async def is_user_authorized(self):
        return self._authorized

    async def get_me(self):
        return self._me

    def add_event_handler(self, callback, event=None):
        self.handlers.append((callback, event))

    async def iter_messages(self, entity, *, min_id: int = 0, reverse: bool = False, limit=None):
        msgs = [m for m in self._messages if m.id > min_id]
        msgs.sort(key=lambda m: m.id, reverse=not reverse)
        if limit is not None:
            msgs = msgs[:limit]
        for m in msgs:
            yield m


class FakeBot:
    """Stands in for telegram.Bot (send-only usage + health get_me)."""

    def __init__(self, *, username: str = "reply_bot", fail: bool = False):
        self._username = username
        self._fail = fail
        self.sent: list[tuple[int, str]] = []

    async def get_me(self):
        if self._fail:
            raise RuntimeError("invalid token")
        return FakeMe(username=self._username)

    async def send_message(self, chat_id: int, text: str):
        self.sent.append((chat_id, text))
