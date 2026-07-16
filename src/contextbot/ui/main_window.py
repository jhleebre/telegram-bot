"""Main application window: a compact status bar that expands on demand.

**Collapsed is the normal state**, and it is one row:

    (▶)  잠자는 중 — Start를 눌러 깨워주세요                        ●  ⌄
    play  what the bot is doing                              health  expand

That is the whole app most of the time, because that is the whole question most of the time ("is it
on, and is anything wrong?"). The light answers the second half by colour alone; when it is amber or
red, expanding shows the full health panel and the activity log, which is where the answer actually
is.

Three things it deliberately does not do:

- **It does not animate.** The status used to be a large emoji face that pulsed on a loop while the
  bot worked, so the app blinked for as long as it was doing its job.
- **It does not say the same thing twice.** The face, a bold status name, *and* a tagline all
  described one state; the tagline says it best, so it is the one that stayed.
- **It does not reflow.** Every column but the message is fixed-width, so nothing shifts as the text
  or the health changes underneath it.

Styling is self-contained QSS so the look is consistent regardless of the system theme.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core.health import HEALTH_COLORS, HEALTH_UNKNOWN_COLOR, HealthStatus
from ..core.status import STATUS_TAGLINES, BotStatus
from .bot_worker import BotWorker
from .elided_label import ElidedLabel

_HEALTH_INTERVAL_MS = 15_000
_MAX_LOG_BLOCKS = 500

# The row's columns. Everything except the message is **fixed**, and that is the point: sized to
# content, every column moved whenever anything changed underneath it — a status bar that
# rearranges itself while you read it is worse than one that wastes a few pixels.
_PLAY_SIZE = 36
_LIGHT_SIZE = 12
_EXPAND_WIDTH = 26

# The one elastic column, and it is deliberately generous — a short message and a long one should
# both look like they belong. Measured against the taglines rather than picked: the longest is 188px
# ("문제가 생겼어요 — 로그를 확인해주세요"), so below ~200 the window would open with its own status
# message already elided. Live messages can be longer; those elide, which is what the stretch is for.
_MESSAGE_MIN_WIDTH = 300

# Symmetric, and tight. The bar is built by hand rather than with `_card`, whose title label would
# pad the top for nothing, and it is Fixed vertically so the hidden detail panel's stretch cannot
# be handed to it and dumped inside as slack.
_BAR_MARGIN = 8

_STYLESHEET = """
#root {
    background-color: #f4f6fb;
}
QFrame.card {
    background-color: #ffffff;
    border: 1px solid #e6e9f2;
    border-radius: 14px;
}
QLabel#cardTitle {
    font-size: 11px;
    font-weight: 700;
    color: #9aa0b5;
}
QLabel#tagline {
    font-size: 13px;
    color: #3a3f57;
}
QLabel#health {
    font-size: 12px;
    color: #3a3f57;
}
#play {
    border: none;
    border-radius: 18px;
    font-size: 12px;
    color: #ffffff;
    background-color: #2ecc71;
}
#play:hover { background-color: #29b765; }
#play[running="true"] { background-color: #e74c3c; }
#play[running="true"]:hover { background-color: #d1412f; }
#expand {
    border: none;
    background: transparent;
    color: #9aa0b5;
    font-size: 14px;
}
#expand:hover { color: #3a3f57; }
QPlainTextEdit#log {
    background-color: #1e2233;
    color: #cdd3ea;
    border: none;
    border-radius: 10px;
    padding: 8px;
}
"""

# Text-presentation variation selector: without it macOS renders ▶ as the colour emoji ▶️, which is
# the opposite of a plain glyph on a coloured button.
_PLAY_GLYPH = "▶︎"
_STOP_GLYPH = "■︎"


def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setProperty("class", "card")
    frame.setFrameShape(QFrame.NoFrame)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 10, 14, 12)
    layout.setSpacing(6)
    heading = QLabel(title)
    heading.setObjectName("cardTitle")
    layout.addWidget(heading)
    return frame, layout


class MainWindow(QWidget):
    def __init__(self, worker: BotWorker):
        super().__init__()
        self._worker = worker
        self._running = False

        self.setObjectName("root")
        self.setWindowTitle("🤖 Context Bot")
        self.setStyleSheet(_STYLESHEET)
        # **No explicit minimum size, in either direction.** The layout's own minimumSizeHint is the
        # floor, and every attempt to second-guess it here has been the same bug: a constant smaller
        # than what the content needs does not shrink the window, it lets Qt squeeze the children
        # past their own minimums and clip them, silently. It hid the health panel's last two probes
        # at 620 (the layout wanted 768), then the expand button at 460 (the row wants ~460). The
        # layout already knows both numbers.

        # ---- the collapsed row.
        self._play_btn = QPushButton(_PLAY_GLYPH)
        self._play_btn.setObjectName("play")
        self._play_btn.setCursor(Qt.PointingHandCursor)
        self._play_btn.setProperty("running", False)
        self._play_btn.setFixedSize(_PLAY_SIZE, _PLAY_SIZE)
        self._play_btn.setToolTip("시작")
        self._play_btn.clicked.connect(self._on_toggle)

        self._message = ElidedLabel()
        self._message.setObjectName("tagline")
        # `Ignored` so it may shrink below its own text — a QLabel's minimum width is the whole
        # string, so any other policy would shove its neighbours off the row rather than elide — plus
        # an explicit minimum for the row to reserve.
        self._message.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._message.setMinimumWidth(_MESSAGE_MIN_WIDTH)

        # A light, with no label beside it. Green needs no caption, and amber/red have more to say
        # than a row has room for — the summary is the tooltip, and `⌄` has the whole story.
        self._health_light = QLabel()
        self._health_light.setFixedSize(_LIGHT_SIZE, _LIGHT_SIZE)
        self._set_light(HEALTH_UNKNOWN_COLOR, "아직 확인 전이에요")

        self._expand_btn = QPushButton("⌄")
        self._expand_btn.setObjectName("expand")
        self._expand_btn.setCursor(Qt.PointingHandCursor)
        self._expand_btn.setToolTip("자세히 보기")
        self._expand_btn.setFixedWidth(_EXPAND_WIDTH)
        self._expand_btn.clicked.connect(self._on_expand)

        self._bar = QFrame()
        self._bar.setProperty("class", "card")
        self._bar.setFrameShape(QFrame.NoFrame)
        self._bar.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        row = QHBoxLayout(self._bar)
        row.setContentsMargins(_BAR_MARGIN, _BAR_MARGIN, _BAR_MARGIN + 4, _BAR_MARGIN)
        row.setSpacing(12)
        row.addWidget(self._play_btn)
        row.addWidget(self._message, 1)
        row.addWidget(self._health_light)
        row.addWidget(self._expand_btn)

        # ---- the detail, hidden until asked for.
        self._detail = QWidget()
        detail_layout = QVBoxLayout(self._detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(12)

        health_card, health_layout = _card("HEALTH")
        self._health_label = QLabel("아직 확인 전이에요")
        self._health_label.setObjectName("health")
        self._health_label.setFont(QFont("Menlo", 11))
        self._health_label.setWordWrap(True)
        policy = self._health_label.sizePolicy()
        policy.setVerticalPolicy(QSizePolicy.MinimumExpanding)
        policy.setHeightForWidth(True)
        self._health_label.setSizePolicy(policy)
        self._health_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        health_layout.addWidget(self._health_label)
        detail_layout.addWidget(health_card)

        log_card, log_layout = _card("ACTIVITY")
        self._log_view = QPlainTextEdit()
        self._log_view.setObjectName("log")
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(_MAX_LOG_BLOCKS)
        self._log_view.setFont(QFont("Menlo", 10))
        self._log_view.setMinimumHeight(160)
        log_layout.addWidget(self._log_view)
        detail_layout.addWidget(log_card, 1)

        self._detail.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(12)
        layout.addWidget(self._bar)
        layout.addWidget(self._detail, 1)

        self._show_status(BotStatus.STOPPED, "")

        # Wire worker signals.
        worker.status_changed.connect(self._on_status_changed)
        worker.log_line.connect(self._append_log)
        worker.health_ready.connect(self._show_health)
        worker.error.connect(self._on_error)

        # Periodic health polling while running.
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(_HEALTH_INTERVAL_MS)
        self._health_timer.timeout.connect(self._worker.request_health)

        # Open at the size the content actually wants. `show()` gives a top-level window a default
        # initial height rather than its sizeHint, and `adjustSize()` does not undo it — which left
        # dead space under the bar on startup. Deferred for the same reason the collapse is: the
        # layout has to settle first.
        QTimer.singleShot(0, self._shrink_to_fit)

    # ------------------------------------------------------------- helpers
    @property
    def is_expanded(self) -> bool:
        return self._detail.isVisible()

    def _set_light(self, color: str, tooltip: str) -> None:
        self._health_light.setStyleSheet(
            f"background-color: {color}; border-radius: {_LIGHT_SIZE // 2}px;"
        )
        self._health_light.setToolTip(tooltip)

    def _show_status(self, status: BotStatus, message: str) -> None:
        """The row's only text. Falls back to the status's tagline when nothing live is happening."""
        self._message.setText(message or STATUS_TAGLINES[status])

    def _set_running_style(self, running: bool) -> None:
        self._running = running
        self._play_btn.setText(_STOP_GLYPH if running else _PLAY_GLYPH)
        self._play_btn.setToolTip("중지" if running else "시작")
        self._play_btn.setProperty("running", running)
        # Re-polish so the [running] property selector takes effect.
        self._play_btn.style().unpolish(self._play_btn)
        self._play_btn.style().polish(self._play_btn)
        if running:
            self._health_timer.start()
        else:
            self._health_timer.stop()

    def _show_health(self, overall: str, summary: str, detail: str) -> None:
        """Show the health report: a light always, the probes when expanded.

        The panel's height comes from the text rather than being reserved in advance. It used to be
        a flat 96px — "room for the 4 probe lines" — and increment 5's two extra probes fell off the
        bottom, invisible: a word-wrapped QLabel does not tell a layout how tall it needs to be, so
        a long path silently ate the lines below it. The probe whose whole job is to be read before
        you send a recording was the first to vanish.
        """
        self._set_light(HEALTH_COLORS[HealthStatus(overall)], summary)
        label = self._health_label
        label.setText(detail)
        width = label.width() or label.sizeHint().width()
        label.setMinimumHeight(label.heightForWidth(width))

    # ------------------------------------------------------------- UI actions
    def _on_expand(self) -> None:
        expanded = not self.is_expanded
        self._detail.setVisible(expanded)
        self._expand_btn.setText("⌃" if expanded else "⌄")
        self._expand_btn.setToolTip("접기" if expanded else "자세히 보기")
        # Qt grows a window to fit new content but never shrinks it back, so collapsing would leave
        # the frame the expanded height with a row rattling around in it. The layout has to settle
        # first — hence the deferred resize rather than an immediate adjustSize().
        QTimer.singleShot(0, self._shrink_to_fit)

    def _shrink_to_fit(self) -> None:
        self.resize(self.width(), self.sizeHint().height())

    def _on_toggle(self) -> None:
        if self._running:
            self._worker.stop_bot()
            self._set_running_style(False)
        else:
            self._worker.start_bot()
            self._set_running_style(True)
            self._worker.request_health()

    def _on_status_changed(self, status_value: str, message: str) -> None:
        self._show_status(BotStatus(status_value), message)
        # Follow STOPPED too, not just ERROR: the bot can stop itself (e.g. a usage limit halts
        # it), and leaving the button on Stop would force a dead click before Start works.
        if status_value in (BotStatus.ERROR.value, BotStatus.STOPPED.value):
            self._set_running_style(False)

    def _on_error(self, message: str) -> None:
        self._append_log(f"⚠️  {message}")
        self._show_status(BotStatus.ERROR, message)
        self._set_running_style(False)

    def _append_log(self, line: str) -> None:
        self._log_view.appendPlainText(line)

    # ------------------------------------------------------------- lifecycle
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self._health_timer.stop()
        self._worker.shutdown()
        super().closeEvent(event)
