"""What `claude -p /usage` says about the plan's remaining allowance, in the bot's own words.

The engine runs on a Claude subscription, so every pipeline in this app spends from two rolling
windows — a 5-hour session limit and a 7-day one — and hitting either is not an abstract risk:
`handlers/conversation.py` has a whole branch for a review that stalls mid-revision on
:class:`ClaudeUsageLimit`, and the capture handlers defer on it. The owner has no way to see how
close they are from Telegram, which is where they actually are when they send the bot work.

So the idle status reply carries it. `/usage` is a **local** command — it reads this machine's own
session records, makes no model call, and returns in about two seconds — which is what makes it
cheap enough to attach to a reply the owner did not ask a question with.

Two shapes, kept apart on purpose:

- :func:`parse_usage` / :func:`format_usage` are pure text→text, so the whole rendering is testable
  against captured CLI output with no subprocess anywhere near it.
- :func:`usage_summary` is the one impure entry point, and it **never raises**. A usage footnote
  that could cost the owner their status reply would be worth less than no footnote at all.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .claude_cli import ClaudeError, build_engine

logger = logging.getLogger("contextbot.engine.usage")

# Measured against CLI v2.1.187 (`claude -p /usage`):
#
#   Current session: 27% used · resets Jul 20 at 2:50pm (Asia/Seoul)
#   Current week (all models): 5% used · resets Jul 24 at 8pm (Asia/Seoul)
#   Current week (Fable): 0% used
#
# The reset clause is optional in the pattern because the per-model line ships without one, and
# because a window that has nothing left to reset toward may well drop it too — a missing reset is
# not a reason to lose the percentage next to it.
_SESSION_RE = re.compile(r"^Current session:\s*(\d+)%\s*used(?:\s*·\s*resets\s+(.+?))?\s*$", re.M)
_WEEK_RE = re.compile(
    r"^Current week \(all models\):\s*(\d+)%\s*used(?:\s*·\s*resets\s+(.+?))?\s*$", re.M
)
_WEEK_MODEL_RE = re.compile(
    r"^Current week \((?!all models\))([^)]+)\):\s*(\d+)%\s*used(?:\s*·\s*resets\s+(.+?))?\s*$",
    re.M,
)

# "Jul 20 at 2:50pm (Asia/Seoul)" — the minutes are optional because the CLI drops them on the hour
# ("at 8pm"), which is exactly the case a stricter pattern would silently fail to translate.
_RESET_RE = re.compile(
    r"^([A-Z][a-z]{2})\s+(\d{1,2})\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I
)
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass(frozen=True)
class UsageWindow:
    """One rolling limit: how much of it is spent, and when it starts over."""

    percent: int
    resets: str | None = None


@dataclass(frozen=True)
class UsageReport:
    """The three windows `/usage` reports. Any of them may be missing.

    Missing is normal rather than broken: the per-model weekly line only appears for a plan that
    has one, and the CLI is free to reword or drop a line this app does not control.
    """

    session: UsageWindow | None = None
    week: UsageWindow | None = None
    model_name: str | None = None
    model_week: UsageWindow | None = None

    def __bool__(self) -> bool:
        """True when at least one window was understood — i.e. there is something worth showing."""
        return bool(self.session or self.week or self.model_week)


def _window(percent: str, resets: str | None) -> UsageWindow:
    return UsageWindow(percent=int(percent), resets=(resets or "").strip() or None)


def parse_usage(text: str) -> UsageReport:
    """Pull the limit lines out of `/usage` output. Never raises; unknown text yields an empty report.

    Deliberately reads only the lines it recognises and ignores everything else. The command also
    prints a "what's contributing" breakdown that is explicitly approximate and machine-local, and
    which changes shape between releases — matching a percentage anywhere in the output would start
    reporting *that* as a limit the day the CLI reorders its own report.
    """
    session = _SESSION_RE.search(text)
    week = _WEEK_RE.search(text)
    model = _WEEK_MODEL_RE.search(text)
    return UsageReport(
        session=_window(*session.groups()) if session else None,
        week=_window(*week.groups()) if week else None,
        model_name=model.group(1).strip() if model else None,
        model_week=_window(model.group(2), model.group(3)) if model else None,
    )


def format_reset(resets: str | None) -> str | None:
    """Render a reset stamp in Korean, or hand back the CLI's own words when it reads unfamiliar.

    The fallback is the point. The stamp is informational — a date the owner glances at — so a
    wording this pattern has never seen ("resets in 2 hours", a localized month) must still reach
    them verbatim rather than being dropped for not matching.
    """
    if not resets:
        return None
    match = _RESET_RE.match(resets)
    if not match:
        return resets
    month, day, hour, minute, meridiem = match.groups()
    number = _MONTHS.get(month.lower())
    if number is None:
        return resets
    hour24 = int(hour) % 12 + (12 if meridiem.lower() == "pm" else 0)
    period = "오전" if hour24 < 12 else "오후"
    return f"{number}월 {int(day)}일 {period} {hour24 % 12 or 12}:{minute or '00'}"


def _line(label: str, window: UsageWindow) -> str:
    reset = format_reset(window.resets)
    return f"• {label}: {window.percent}% 사용" + (f" — {reset} 리셋" if reset else "")


def format_usage(report: UsageReport) -> str:
    """The usage block as it appears in a bot DM. Plain text, by the same rule as every other reply.

    `Notifier.send` passes no parse_mode (see `handlers/conversation.py`), so nothing here may lean
    on Markdown — the bullets are literal characters and are meant to be.
    """
    lines = ["📊 사용량 (이 기기 기준)"]
    if report.session:
        lines.append(_line("5시간 한도", report.session))
    if report.week:
        lines.append(_line("7일 한도", report.week))
    # Only when it has actually started: a per-model weekly line sits at 0% for anyone not using
    # that model, and a permanent "0% 사용" row is noise in a message the owner reads at a glance.
    if report.model_week and report.model_week.percent:
        lines.append(_line(f"7일 한도 ({report.model_name})", report.model_week))
    return "\n".join(lines)


async def usage_summary(settings, *, timeout_sec: float = 30.0) -> str:
    """Read and render the current usage. **Never raises** — returns a one-line note on failure.

    Says so rather than staying quiet, because the block's absence would otherwise be ambiguous in
    the one direction that matters: an owner near their limit and an owner whose CLI could not be
    reached would see the identical message.
    """
    try:
        text = await build_engine(settings).usage_text(timeout_sec=timeout_sec)
    except ClaudeError as exc:
        logger.warning("could not read usage: %s", exc)
        return f"📊 사용량은 확인하지 못했습니다 — {exc}"

    report = parse_usage(text)
    if not report:
        logger.warning("usage output had no limit lines: %r", text[:200])
        return "📊 사용량은 확인하지 못했습니다 — /usage 출력을 읽지 못했습니다."
    return format_usage(report)
