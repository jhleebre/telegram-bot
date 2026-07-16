"""The row's two buttons, drawn rather than typed.

**Both were text glyphs, and both were wrong for the same reason: a font is not a shape library.**

- `▶` and `■` needed a text-presentation variation selector or macOS rendered them as colour emoji —
  a coloured picture on top of a coloured button.
- `⌄` and `⌃` are *different characters* (U+2304 DOWN ARROWHEAD, U+2303 UP ARROWHEAD). They look
  like a pair in a table and are not one in a typeface: different weights, different sizes, and
  different baselines, so the chevron changed shape and jumped when the panel opened.

Painting them removes the font from the question entirely. The chevron is drawn **once** and the
painter is rotated 180° for the other direction, so up and down are the same shape by construction
rather than by a font's good intentions — which is what the owner asked for.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QPushButton, QWidget

# macOS system colours rather than web-palette approximations: this is a Mac app, and #2ecc71 next
# to a native control reads as slightly-wrong green.
SYSTEM_GREEN = "#34C759"
SYSTEM_RED = "#FF3B30"

_CHEVRON_COLOR = "#A0A4B8"
_CHEVRON_HOVER = "#5A5F78"

_BUTTON_SIZE = 28


class PlayStopButton(QPushButton):
    """A round button carrying a bare triangle or square, and nothing else.

    Small on purpose. It was 36px of saturated fill — the loudest thing in a window whose actual
    news is the line of text beside it, and this is a status bar, not a transport control.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._running = False
        self.setFixedSize(_BUTTON_SIZE, _BUTTON_SIZE)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("시작")
        self._hover = False

    def set_running(self, running: bool) -> None:
        self._running = running
        self.setToolTip("중지" if running else "시작")
        self.update()

    @property
    def is_running(self) -> bool:
        return self._running

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        fill = QColor(SYSTEM_RED if self._running else SYSTEM_GREEN)
        if self._hover:
            fill = fill.darker(112)
        painter.setPen(Qt.NoPen)
        painter.setBrush(fill)
        painter.drawEllipse(self.rect())

        painter.setBrush(QColor("#ffffff"))
        painter.translate(self.width() / 2.0, self.height() / 2.0)
        if self._running:
            side = 8.0
            painter.drawRoundedRect(
                -side / 2.0, -side / 2.0, side, side, 1.2, 1.2
            )
        else:
            width, height = 9.0, 10.0
            # Nudged right by width/6. A triangle centred on its *bounding box* looks left-heavy,
            # because its visual mass is at the centroid — which sits a third of the way from the
            # base, not halfway. Every play button you have ever thought looked right does this.
            painter.translate(width / 6.0, 0.0)
            painter.drawPolygon(
                [
                    QPointF(-width / 2.0, -height / 2.0),
                    QPointF(-width / 2.0, height / 2.0),
                    QPointF(width / 2.0, 0.0),
                ]
            )


class ChevronButton(QPushButton):
    """A disclosure chevron. One shape, rotated — never two characters.

    Sized to match :class:`PlayStopButton`, so the row's two controls share a height and the layout
    can centre them against each other rather than against two fonts' baselines.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._up = False
        self._hover = False
        self.setFixedSize(_BUTTON_SIZE, _BUTTON_SIZE)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("자세히 보기")

    def set_pointing_up(self, up: bool) -> None:
        self._up = up
        self.setToolTip("접기" if up else "자세히 보기")
        self.update()

    @property
    def points_up(self) -> bool:
        return self._up

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        pen = QPen(QColor(_CHEVRON_HOVER if self._hover else _CHEVRON_COLOR))
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        painter.translate(self.width() / 2.0, self.height() / 2.0)
        if self._up:
            # The same path, turned over. Two glyphs could never be this symmetric.
            painter.rotate(180.0)
        arm, drop = 4.5, 2.4
        painter.drawPolyline(
            [QPointF(-arm, -drop / 2.0), QPointF(0.0, drop / 2.0 + 0.6), QPointF(arm, -drop / 2.0)]
        )
