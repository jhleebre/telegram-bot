"""Async wrapper over Claude Code's headless CLI (``claude -p``).

This is the shared engine for every Phase 2 pipeline (text enrichment, image description,
document structuring, meeting notes). It is deliberately **text-in / text-out**:

- The prompt is written to the subprocess's **stdin**, not passed as an argv element, so long
  inputs (meeting transcripts, extracted documents) cannot hit ``ARG_MAX``.
- Claude is never granted write access. Per the Phase 2 open decision *"file-writing
  responsibility"*, the **bot writes all files**; Claude only returns text. That keeps the
  pipelines testable and means no permissive ``--permission-mode`` is needed.
- ``session_id`` is captured from the JSON result so a later turn can ``--resume`` the same
  session with full prior context — the basis of the Phase 2 human-in-the-loop review.

Everything is non-blocking (``asyncio.create_subprocess_exec``) so the UI keeps repainting and
the Telethon loop keeps running while a job is in flight.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger("contextbot.engine.claude")

DEFAULT_EXECUTABLE = "claude"
DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT_SEC = 120.0

# A GUI app launched from the Dock/Finder inherits launchd's minimal PATH
# (/usr/local/bin:/bin:/usr/bin) — not the login shell's — so `claude` installed by Homebrew or
# the native installer is invisible to shutil.which. These are the standard install locations,
# checked only after PATH lookup fails. Set CLAUDE_BIN for anything unusual (e.g. nvm/npm).
_FALLBACK_BIN_DIRS = (
    "/opt/homebrew/bin",  # Homebrew (Apple Silicon)
    "/usr/local/bin",  # Homebrew (Intel) / manual install
    "~/.claude/local",  # Claude Code native installer
    "~/.local/bin",
)


def resolve_executable(executable: str = DEFAULT_EXECUTABLE) -> str | None:
    """Return an absolute path to the CLI, or None when it cannot be found.

    Accepts a bare name (resolved via PATH, then the known install dirs) or an explicit path.
    """
    if os.sep in executable or executable.startswith("~"):
        candidate = Path(executable).expanduser()
        return str(candidate) if _is_executable(candidate) else None

    found = shutil.which(executable)
    if found:
        return found

    for directory in _FALLBACK_BIN_DIRS:
        candidate = Path(directory).expanduser() / executable
        if _is_executable(candidate):
            logger.debug("resolved %r outside PATH: %s", executable, candidate)
            return str(candidate)
    return None


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


class ClaudeError(Exception):
    """Base class for engine failures."""


class ClaudeUnavailable(ClaudeError):
    """The ``claude`` executable is not installed or not on PATH."""


class ClaudeTimeout(ClaudeError):
    """The job exceeded its timeout budget and was killed."""


class ClaudeUsageLimit(ClaudeError):
    """The API refused the request for quota reasons (HTTP 429).

    Covers both a temporary server-side rate limit and an exhausted subscription usage limit —
    the CLI reports both as 429 and distinguishes them only in the message, which is preserved in
    ``str(exc)``. Either way the caller should degrade rather than retry immediately: a
    subscription limit can persist for hours.
    """


def _error_from(data: dict[str, Any], returncode: int) -> ClaudeError:
    """Build the most specific error the CLI's JSON result supports."""
    message = str(data.get("result") or "").strip() or f"claude exited {returncode}"
    subtype = data.get("subtype")
    if subtype and subtype != "success":
        message = f"{subtype}: {message}"
    status = data.get("api_error_status")
    if status == 429:
        return ClaudeUsageLimit(message)
    if status is not None:
        return ClaudeError(f"API error {status}: {message}")
    return ClaudeError(message)


@dataclass(frozen=True)
class ClaudeResult:
    """A parsed ``--output-format json`` result."""

    text: str
    session_id: str | None = None
    # `total_cost_usd`: an *estimate* the CLI computes locally from token counts at standard API
    # rates — not an amount billed. Under a Pro/Max subscription the run consumes plan usage
    # limits, not money, so treat this as a proxy for how fast the plan's allowance is spent.
    cost_usd: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0
    # Full decoded JSON, kept for diagnostics; excluded from equality/repr.
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


def _decode(stdout: bytes) -> dict[str, Any]:
    """Decode the CLI's JSON result object, raising ClaudeError on malformed output."""
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        raise ClaudeError("claude produced no output")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClaudeError(f"claude returned non-JSON output: {text[:200]!r}") from exc
    if not isinstance(data, dict):
        raise ClaudeError(f"claude returned unexpected JSON: {type(data).__name__}")
    return data


class ClaudeCLI:
    """Runs headless ``claude -p`` jobs and parses their JSON results.

    Injectable into handlers so tests can substitute a fake without spawning a real model run.
    """

    def __init__(
        self,
        *,
        executable: str = DEFAULT_EXECUTABLE,
        model: str = DEFAULT_MODEL,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        cwd: Path | None = None,
    ) -> None:
        self.executable = executable
        self.model = model
        self.timeout_sec = timeout_sec
        self.cwd = cwd

    def resolve(self) -> str | None:
        """Absolute path to the CLI this engine will run, or None if it is not installed."""
        return resolve_executable(self.executable)

    def is_available(self) -> bool:
        """True when the CLI can be located (PATH or a known install dir)."""
        return self.resolve() is not None

    def _build_argv(
        self,
        executable: str,
        *,
        system_prompt: str | None,
        add_dirs: Sequence[Path],
        resume: str | None,
        session_id: str | None,
    ) -> list[str]:
        # No prompt argv element: the prompt goes to stdin (see module docstring).
        argv = [executable, "-p", "--output-format", "json", "--model", self.model]
        if system_prompt is not None:
            argv += ["--system-prompt", system_prompt]
        if resume is not None:
            argv += ["--resume", resume]
        elif session_id is not None:
            argv += ["--session-id", session_id]
        for directory in add_dirs:
            argv += ["--add-dir", str(directory)]
        # Keep runs hermetic and cheap: no MCP servers, no user/project settings, no skills.
        argv += ["--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands"]
        return argv

    async def run(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        add_dirs: Sequence[Path] = (),
        session_id: str | None = None,
        resume: str | None = None,
        timeout_sec: float | None = None,
    ) -> ClaudeResult:
        """Run one headless job and return its parsed result.

        ``resume`` continues an existing session (Phase 2 review turns); ``session_id`` pins a new
        session to a caller-chosen UUID. Raises :class:`ClaudeUnavailable`, :class:`ClaudeTimeout`,
        or :class:`ClaudeError`.
        """
        executable = self.resolve()
        if executable is None:
            raise ClaudeUnavailable(
                f"{self.executable!r} not found on PATH or in {', '.join(_FALLBACK_BIN_DIRS)} "
                "(set CLAUDE_BIN to its full path)"
            )

        argv = self._build_argv(
            executable,
            system_prompt=system_prompt,
            add_dirs=add_dirs,
            resume=resume,
            session_id=session_id,
        )
        budget = timeout_sec if timeout_sec is not None else self.timeout_sec

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.cwd) if self.cwd else None,
            )
        except FileNotFoundError as exc:  # raced with is_available, or bad absolute path
            raise ClaudeUnavailable(f"{self.executable!r} could not be executed") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")), timeout=budget
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # Kill the child so a cancelled/late job cannot outlive the request.
            await self._terminate(proc)
            raise ClaudeTimeout(f"claude exceeded {budget:.0f}s") from None

        # An API failure (rate limit, outage) exits non-zero with an *empty stderr* and puts the
        # useful detail in the stdout JSON, so parse it before falling back to the exit code.
        data: dict[str, Any] | None = None
        try:
            data = _decode(stdout)
        except ClaudeError:
            if proc.returncode == 0:
                raise
        if data is None:
            detail = stderr.decode("utf-8", errors="replace").strip()[:300]
            raise ClaudeError(f"claude exited {proc.returncode}: {detail or '<no stderr>'}")

        # `subtype` stays "success" even when is_error is true (verified against CLI v2.1.187),
        # so is_error and the exit code — not subtype — are the reliable signals. The subtype
        # check is kept as defense for result kinds that may not set is_error (e.g. max turns).
        if (
            data.get("is_error")
            or proc.returncode != 0
            or data.get("subtype") not in (None, "success")
        ):
            raise _error_from(data, proc.returncode)

        result = ClaudeResult(
            text=(data.get("result") or "").strip(),
            session_id=data.get("session_id"),
            cost_usd=float(data.get("total_cost_usd") or 0.0),
            duration_ms=int(data.get("duration_ms") or 0),
            num_turns=int(data.get("num_turns") or 0),
            raw=data,
        )
        # `total_cost_usd` is what the job *would* have cost at standard API rates — the CLI
        # computes it locally from token counts. On a Pro/Max subscription nothing is charged for
        # it; the run draws from the plan's usage limits instead (shared with claude.ai and other
        # Claude Code sessions). Logged as "~$" and "est" so it never reads as a bill.
        logger.info(
            "claude job done: model=%s turns=%d %dms ~$%.4f est session=%s",
            self.model,
            result.num_turns,
            result.duration_ms,
            result.cost_usd,
            result.session_id,
        )
        return result

    async def resume_session(self, session_id: str, prompt: str, **kwargs: Any) -> ClaudeResult:
        """Continue an existing session with a new turn (keeps full prior context)."""
        return await self.run(prompt, resume=session_id, **kwargs)

    @staticmethod
    async def _terminate(proc: asyncio.subprocess.Process) -> None:
        """Kill a still-running child and reap it, ignoring races where it already exited."""
        if proc.returncode is not None:
            return
        try:
            proc.kill()
        except ProcessLookupError:  # pragma: no cover - exited between check and kill
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:  # pragma: no cover - unkillable child
            logger.warning("claude subprocess did not exit after kill")


def build_engine(settings) -> ClaudeCLI:
    """Construct a :class:`ClaudeCLI` from :class:`~contextbot.config.Settings`."""
    return ClaudeCLI(
        executable=settings.claude_bin,
        model=settings.claude_model,
        timeout_sec=settings.claude_timeout_sec,
    )
