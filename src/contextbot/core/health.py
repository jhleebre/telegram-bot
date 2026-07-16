"""Health checks: Telethon auth, bot token, inbox writability, connection state, LLM engine."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from ..engine.claude_cli import build_engine, resolve_executable


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    ERROR = "error"


@dataclass
class ProbeResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class HealthReport:
    overall: HealthStatus
    probes: list[ProbeResult] = field(default_factory=list)

    def probe(self, name: str) -> Optional[ProbeResult]:
        for p in self.probes:
            if p.name == name:
                return p
        return None

    def as_text(self) -> str:
        icon = {
            HealthStatus.HEALTHY: "🟢",
            HealthStatus.DEGRADED: "🟡",
            HealthStatus.ERROR: "🔴",
        }[self.overall]
        lines = [f"{icon} Health: {self.overall.value}"]
        for p in self.probes:
            mark = "✅" if p.ok else "❌"
            detail = f" — {p.detail}" if p.detail else ""
            lines.append(f"{mark} {p.name}{detail}")
        return "\n".join(lines)


def _inbox_writable(inbox_dir: Path) -> ProbeResult:
    if not inbox_dir.exists():
        return ProbeResult("inbox", False, f"directory does not exist: {inbox_dir}")
    if not inbox_dir.is_dir():
        return ProbeResult("inbox", False, f"not a directory: {inbox_dir}")
    try:
        fd, tmp = tempfile.mkstemp(dir=str(inbox_dir), suffix=".healthcheck")
        os.close(fd)
        os.unlink(tmp)
    except OSError as exc:
        return ProbeResult("inbox", False, f"not writable: {exc}")
    return ProbeResult("inbox", True, str(inbox_dir))


async def _claude_engine(settings) -> ProbeResult:
    """Probe the Phase 2 LLM engine.

    Never fatal: notes are still captured without it (handlers fall back to the no-LLM path), so a
    missing CLI or a logged-out one is DEGRADED, not ERROR.

    Two failure modes are surfaced here, both otherwise silent:

    - **Not findable.** A Dock-launched app inherits a minimal PATH and can miss a `claude` that
      works in the terminal (the increment-1 bug). Reporting the resolved path makes it visible.
    - **Findable but logged out.** A resolvable binary whose login has expired fails every job in
      ~50ms with `Not logged in`, indistinguishable downstream from a working engine — this is what
      bit the first PDF run. `check_auth` catches it with a local, no-usage `auth status` call.
    """
    if not settings.claude_enabled:
        return ProbeResult("claude-engine", True, "disabled (CLAUDE_ENABLED=false)")
    engine = build_engine(settings)
    resolved = engine.resolve()
    if resolved is None:
        return ProbeResult(
            "claude-engine", False, f"{settings.claude_bin!r} not found — notes saved without LLM"
        )
    if await engine.check_auth() is False:
        return ProbeResult(
            "claude-engine",
            False,
            f"{resolved} — logged out (run: claude, then /login)",
        )
    # None (undeterminable) is treated as OK: don't cry wolf when we can't tell.
    return ProbeResult("claude-engine", True, f"{resolved} ({settings.claude_model})")


def _whisper_stt(settings) -> ProbeResult:
    """Probe the local STT stack: the package, the **weights**, and ffmpeg.

    Never fatal — every other pipeline works without it, so a missing piece is DEGRADED.

    **The weights are the reason this probe exists** (docs/PHASE2.md, increment 5). The model is a
    HuggingFace repo id rather than a package dependency, so `pip install` does not bring it and
    nobody ever installs it: `mlx_whisper` fetches ~1.5GB on first use, silently, *inside* whatever
    job asked first. That job is a meeting note the owner is waiting on — minutes of unexplained
    stall, or an outright failure offline. This turns it into a line on the health panel they can
    act on before they send a recording, and it costs nothing: the check is local and offline.

    On this machine it will always read green (meeting-transcriber cached the weights years-deep),
    which is exactly why it is written down rather than trusted to be noticed. Same shape as
    increment 1's Dock PATH bug: unreproducible where it was developed.
    """
    from ..stt import whisper

    if not whisper.package_is_installed():
        return ProbeResult(
            "whisper-stt", False, "mlx-whisper not installed — audio notes unavailable"
        )
    if not whisper.model_is_cached(settings.whisper_model):
        return ProbeResult(
            "whisper-stt",
            False,
            f"model not downloaded ({settings.whisper_model}, {whisper.MODEL_SIZE_HINT}) — "
            "run: .venv/bin/python scripts/download_model.py",
        )
    ffmpeg = whisper.resolve_ffmpeg()
    if ffmpeg is None:
        # Resolved the increment-1 way, so this reports what a *Dock-launched* app would see —
        # which is the only launch that has ever hit this bug.
        return ProbeResult("whisper-stt", False, "ffmpeg not found — install: brew install ffmpeg")
    return ProbeResult("whisper-stt", True, f"{settings.whisper_model} · ffmpeg {ffmpeg}")


def _glossary(settings) -> ProbeResult:
    """Probe the meeting glossary. Absent is **not** a failure — it is a valid, quiet state.

    Reported anyway because it is otherwise invisible: a glossary at the wrong path means every
    meeting note silently loses its term corrections, and nothing else would ever say so.
    """
    path = settings.glossary_path
    if not path.is_file():
        return ProbeResult("glossary", True, f"none at {path} — meeting notes skip term corrections")
    return ProbeResult("glossary", True, f"{path} ({path.stat().st_size // 1024}KB)")


class HealthChecker:
    """Runs the health probes against the Telethon client, the reply bot, and settings."""

    def __init__(self, client, bot, settings):
        self._client = client
        self._bot = bot
        self._settings = settings

    async def _auth_probe(self) -> ProbeResult:
        try:
            authorized = await self._client.is_user_authorized()
        except Exception as exc:  # noqa: BLE001
            return ProbeResult("telethon-auth", False, f"{type(exc).__name__}: {exc}")
        if not authorized:
            return ProbeResult("telethon-auth", False, "not logged in (run login.py)")
        return ProbeResult("telethon-auth", True, "authorized")

    async def _token_probe(self) -> ProbeResult:
        if self._bot is None:
            return ProbeResult("bot-token", False, "no bot configured")
        try:
            me = await self._bot.get_me()
        except Exception as exc:  # noqa: BLE001
            return ProbeResult("bot-token", False, f"{type(exc).__name__}: {exc}")
        username = getattr(me, "username", None)
        return ProbeResult("bot-token", True, f"@{username}" if username else "valid")

    def _connection_probe(self) -> ProbeResult:
        try:
            connected = bool(self._client.is_connected())
        except Exception as exc:  # noqa: BLE001
            return ProbeResult("connection", False, f"{type(exc).__name__}: {exc}")
        return ProbeResult("connection", connected, "connected" if connected else "disconnected")

    async def check(self) -> HealthReport:
        auth = await self._auth_probe()
        token = await self._token_probe()
        inbox = _inbox_writable(self._settings.inbox_dir)
        connection = self._connection_probe()
        claude = await _claude_engine(self._settings)
        stt = _whisper_stt(self._settings)
        glossary = _glossary(self._settings)

        if not auth.ok or not token.ok:
            overall = HealthStatus.ERROR
        elif not inbox.ok or not claude.ok or not stt.ok:
            overall = HealthStatus.DEGRADED
        else:
            overall = HealthStatus.HEALTHY

        return HealthReport(
            overall=overall, probes=[auth, token, inbox, connection, claude, stt, glossary]
        )
