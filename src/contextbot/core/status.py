"""UI-agnostic bot status model.

The GUI subscribes to :class:`StatusModel` and renders whatever it publishes; nothing here
depends on Qt, so it is fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class BotStatus(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PROCESSING = "processing"
    ERROR = "error"


# Color hint per status, consumed by the UI status widget.
STATUS_COLORS: dict[BotStatus, str] = {
    BotStatus.STOPPED: "#9e9e9e",     # gray
    BotStatus.STARTING: "#f5a623",    # amber
    BotStatus.RUNNING: "#2ecc71",     # green
    BotStatus.PROCESSING: "#3498db",  # blue
    BotStatus.ERROR: "#e74c3c",       # red
}

STATUS_LABELS: dict[BotStatus, str] = {
    BotStatus.STOPPED: "Stopped",
    BotStatus.STARTING: "Starting…",
    BotStatus.RUNNING: "Running",
    BotStatus.PROCESSING: "Processing…",
    BotStatus.ERROR: "Error",
}

# Friendly emoji per status, shown in the UI status "face".
STATUS_EMOJI: dict[BotStatus, str] = {
    BotStatus.STOPPED: "😴",
    BotStatus.STARTING: "🔌",
    BotStatus.RUNNING: "🤖",
    BotStatus.PROCESSING: "✍️",
    BotStatus.ERROR: "🚨",
}

# A short, friendly one-liner per status (used when no live message is present).
STATUS_TAGLINES: dict[BotStatus, str] = {
    BotStatus.STOPPED: "잠자는 중 — Start를 눌러 깨워주세요",
    BotStatus.STARTING: "깨어나는 중…",
    BotStatus.RUNNING: "메시지를 기다리는 중이에요",
    BotStatus.PROCESSING: "열심히 정리하고 있어요…",
    BotStatus.ERROR: "문제가 생겼어요 — 로그를 확인해주세요",
}


Observer = Callable[[BotStatus, str], None]


@dataclass
class StatusModel:
    """Holds the current status and notifies observers on change."""

    status: BotStatus = BotStatus.STOPPED
    message: str = ""
    _observers: list[Observer] = field(default_factory=list, repr=False)

    def subscribe(self, observer: Observer) -> None:
        self._observers.append(observer)

    def unsubscribe(self, observer: Observer) -> None:
        if observer in self._observers:
            self._observers.remove(observer)

    def set(self, status: BotStatus, message: str = "") -> None:
        """Update the status and notify observers.

        Observers are always notified (even if the status value is unchanged) so that a new
        message accompanying the same status still propagates.
        """
        self.status = status
        self.message = message
        for observer in list(self._observers):
            observer(status, message)

    @property
    def color(self) -> str:
        return STATUS_COLORS[self.status]

    @property
    def label(self) -> str:
        return STATUS_LABELS[self.status]
