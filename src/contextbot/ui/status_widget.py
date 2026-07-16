"""The status indicator: a static coloured dot with a status emoji, plus one line of text.

Sized to sit in the window's single collapsed row (see `main_window`), so it is deliberately small
and horizontal — the big centred "face" it replaces belonged to a window that was mostly empty.

**It does not animate.** An earlier version pulsed the dot's opacity on a loop while the bot was
active, which meant the app blinked at the owner for as long as it was doing its job. Motion in a
status light should mean *something changed*; a permanent pulse only means "still on", which the
colour already says without moving. Kept UI-only; all state comes from :class:`BotStatus`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

from ..core.status import STATUS_COLORS, STATUS_EMOJI, STATUS_TAGLINES, BotStatus
from .elided_label import ElidedLabel

_DOT_SIZE = 30
# The row's one elastic column: it reserves this much and takes whatever else is going. Everything
# beside it is fixed-width, so this is the only thing that changes size — which is the point (see
# main_window's row).
#
# Measured against the taglines rather than picked: the longest is 188px ("문제가 생겼어요 — 로그를
# 확인해주세요"). Below that, the *default* window would open with its own status message already
# elided, which is a strange thing for an app to do to itself. Live messages can still be longer,
# and those elide — that is what the column is elastic for.
_MESSAGE_MIN_WIDTH = 200


class StatusWidget(QWidget):
    """`[🤖] 메시지를 기다리는 중이에요` — one row, static."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        self._light = QLabel()
        self._light.setFixedSize(_DOT_SIZE, _DOT_SIZE)
        self._light.setAlignment(Qt.AlignCenter)
        face_font = self._light.font()
        face_font.setPointSize(13)
        self._light.setFont(face_font)

        # The live message ("검토 반영 중…") or the status's own tagline — **the only text here**.
        # The old layout also carried the status *name* ("Processing…") beside it, which said the
        # same thing as the dot it sat next to and then squeezed the message that actually carries
        # news into an ellipsis. The dot's colour and emoji are the state; this says what the bot is
        # doing about it.
        self._message = ElidedLabel()
        self._message.setObjectName("tagline")
        # `Ignored` so it may shrink below its own text (a QLabel's minimum width is the whole
        # string, so anything else would shove its neighbours off the row rather than elide), and an
        # explicit minimum so the row still reserves a readable amount for it.
        self._message.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._message.setMinimumWidth(_MESSAGE_MIN_WIDTH)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self._light)
        layout.addWidget(self._message, 1)

        self.set_status(BotStatus.STOPPED, "")

    def _apply_face(self, status: BotStatus) -> None:
        self._light.setStyleSheet(
            f"background-color: {STATUS_COLORS[status]};"
            f"border-radius: {_DOT_SIZE // 2}px;"
            "border: 2px solid rgba(255,255,255,0.65);"
        )
        self._light.setText(STATUS_EMOJI[status])

    def set_status(self, status: BotStatus, message: str = "") -> None:
        self._apply_face(status)
        self._message.setText(message or STATUS_TAGLINES[status])

    def set_status_value(self, status_value: str, message: str = "") -> None:
        """Convenience for the signal payload (status is passed as its string value)."""
        self.set_status(BotStatus(status_value), message)
