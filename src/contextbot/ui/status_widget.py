"""A friendly, graphical status indicator: a big colored 'face' emoji + label + tagline.

Gently pulses while the bot is active. Kept UI-only; all state comes from :class:`BotStatus`.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel, QVBoxLayout, QWidget

from ..core.status import (
    STATUS_COLORS,
    STATUS_EMOJI,
    STATUS_LABELS,
    STATUS_TAGLINES,
    BotStatus,
)

_FACE_SIZE = 96
_ACTIVE = {BotStatus.STARTING, BotStatus.RUNNING, BotStatus.PROCESSING}


class StatusWidget(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        # The circular "face" showing a status emoji on a colored disc.
        self._light = QLabel()
        self._light.setFixedSize(_FACE_SIZE, _FACE_SIZE)
        self._light.setAlignment(Qt.AlignCenter)
        face_font = self._light.font()
        face_font.setPointSize(40)
        self._light.setFont(face_font)

        # Bold status label (e.g. "Running").
        self._text = QLabel()
        self._text.setAlignment(Qt.AlignCenter)
        label_font = self._text.font()
        label_font.setPointSize(label_font.pointSize() + 5)
        label_font.setBold(True)
        self._text.setFont(label_font)

        # Secondary, friendly tagline / live message.
        self._message = QLabel()
        self._message.setAlignment(Qt.AlignCenter)
        self._message.setWordWrap(True)
        self._message.setObjectName("tagline")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(8)
        layout.addWidget(self._light, alignment=Qt.AlignHCenter)
        layout.addWidget(self._text)
        layout.addWidget(self._message)

        # Soft pulsing while active.
        self._opacity = QGraphicsOpacityEffect(self._light)
        self._opacity.setOpacity(1.0)
        self._light.setGraphicsEffect(self._opacity)
        self._pulse = QPropertyAnimation(self._opacity, b"opacity", self)
        self._pulse.setDuration(1100)
        self._pulse.setStartValue(1.0)
        self._pulse.setKeyValueAt(0.5, 0.45)
        self._pulse.setEndValue(1.0)
        self._pulse.setLoopCount(-1)
        self._pulse.setEasingCurve(QEasingCurve.InOutSine)

        self.set_status(BotStatus.STOPPED, "")

    def _apply_face(self, status: BotStatus) -> None:
        color = STATUS_COLORS[status]
        radius = _FACE_SIZE // 2
        self._light.setStyleSheet(
            f"background-color: {color};"
            f"border-radius: {radius}px;"
            "border: 3px solid rgba(255,255,255,0.65);"
        )
        self._light.setText(STATUS_EMOJI[status])

    def _update_pulse(self, status: BotStatus) -> None:
        if status in _ACTIVE:
            if self._pulse.state() != QPropertyAnimation.Running:
                self._pulse.start()
        else:
            self._pulse.stop()
            self._opacity.setOpacity(1.0)

    def set_status(self, status: BotStatus, message: str = "") -> None:
        self._apply_face(status)
        self._text.setText(STATUS_LABELS[status])
        self._message.setText(message or STATUS_TAGLINES[status])
        self._update_pulse(status)

    def set_status_value(self, status_value: str, message: str = "") -> None:
        """Convenience for the signal payload (status is passed as its string value)."""
        self.set_status(BotStatus(status_value), message)
