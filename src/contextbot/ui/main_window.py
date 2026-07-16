"""Main application window: a compact status bar that expands on demand.

**Collapsed is the normal state**, and it is one row — status dot, what the bot is doing, the
Start/Stop button, and a one-line health summary. That is the whole app most of the time, because
that is the whole question most of the time ("is it on, and is anything wrong?"). Expanding drops
the full health panel and the activity log underneath it, which is where you go when the summary
says something is wrong.

The earlier layout showed all of it, always: a large centred status face, seven probe lines, and a
log pane, in a window that was 600px of mostly-empty card. It also *blinked* — the face pulsed on a
loop whenever the bot was working. Both are gone.

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

from ..core.status import BotStatus
from .bot_worker import BotWorker
from .elided_label import ElidedLabel
from .status_widget import StatusWidget

_HEALTH_INTERVAL_MS = 15_000
_MAX_LOG_BLOCKS = 500

# The collapsed row's columns are **fixed**, and that is the whole point of these numbers. Sized to
# content, every column moved whenever anything changed: `🟢 정상` → `🟡 whisper-stt` shoved the
# Start button 51px left, and `▶ Start` → `■ Stop` twitched it another 2px. A status bar that
# rearranges itself while you read it is worse than one that wastes a little space, so each column
# is wide enough for its worst case and the message column absorbs all the slack.
_BUTTON_WIDTH = 96      # fits "▶  Start" and "■  Stop" identically
_HEALTH_WIDTH = 150     # fits "🟡 whisper-stt"; longer lists elide (the tooltip and ⌄ have them all)
_EXPAND_WIDTH = 26

# Symmetric, and tight. The bar used to be built with `_card("")` — an empty heading label plus its
# spacing, padding the top for nothing — and then the window layout handed it the *hidden* detail
# panel's stretch, so it grew past its own sizeHint and dumped the slack underneath: 16px above the
# row, 21px below. Both are gone: no heading, and the bar is Fixed vertically.
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
    font-size: 12px;
    color: #7b8199;
}
QLabel#healthSummary {
    font-size: 12px;
    font-weight: 600;
    color: #3a3f57;
}
QLabel#health {
    font-size: 12px;
    color: #3a3f57;
}
#toggle {
    border: none;
    border-radius: 15px;
    padding: 7px 16px;
    font-size: 13px;
    font-weight: 800;
    color: #ffffff;
    background-color: #2ecc71;
}
#toggle:hover { background-color: #29b765; }
#toggle[running="true"] { background-color: #e74c3c; }
#toggle[running="true"]:hover { background-color: #d1412f; }
#expand {
    border: none;
    background: transparent;
    color: #9aa0b5;
    font-size: 14px;
    padding: 4px 6px;
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
        # floor, and every attempt to second-guess it here has been the same bug three times over: a
        # constant smaller than what the content needs does not shrink the window, it lets Qt squeeze
        # the children past their own minimums and clip them, silently. It hid the health panel's
        # last two probes at 620 (the layout wanted 768), and then hid the expand button and half the
        # health summary at 460 (the row wants 592). The layout already knows both numbers.

        # ---- the collapsed row: everything the owner normally needs, and nothing else.
        self._status_widget = StatusWidget()

        self._toggle_btn = QPushButton("▶  Start")
        self._toggle_btn.setObjectName("toggle")
        self._toggle_btn.setCursor(Qt.PointingHandCursor)
        self._toggle_btn.setProperty("running", False)
        self._toggle_btn.setFixedWidth(_BUTTON_WIDTH)
        self._toggle_btn.clicked.connect(self._on_toggle)

        self._health_summary = ElidedLabel("—")
        self._health_summary.setObjectName("healthSummary")
        self._health_summary.setFixedWidth(_HEALTH_WIDTH)

        self._expand_btn = QPushButton("⌄")
        self._expand_btn.setObjectName("expand")
        self._expand_btn.setCursor(Qt.PointingHandCursor)
        self._expand_btn.setToolTip("자세히 보기")
        self._expand_btn.setFixedWidth(_EXPAND_WIDTH)
        self._expand_btn.clicked.connect(self._on_expand)

        # Built by hand rather than with `_card`: that helper adds a title label, and a card whose
        # title is "" is an empty label silently padding the top of the row.
        bar = QFrame()
        bar.setProperty("class", "card")
        bar.setFrameShape(QFrame.NoFrame)
        # Fixed height, or the layout gives it the hidden detail panel's stretch and it grows past
        # its own sizeHint — with the slack landing under the row, not around it.
        bar.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, _BAR_MARGIN, _BAR_MARGIN, _BAR_MARGIN)
        row.setSpacing(10)
        row.addWidget(self._status_widget, 1)
        row.addWidget(self._toggle_btn)
        row.addWidget(self._health_summary)
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
        layout.addWidget(bar)
        layout.addWidget(self._detail, 1)

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
        # initial height (100px here) rather than its sizeHint (74), and `adjustSize()` does not
        # undo it — which left 26px of dead space under the bar on startup, i.e. exactly the
        # lopsided padding this row was tightened to remove. Deferred for the same reason the
        # collapse is: the layout has to settle first.
        QTimer.singleShot(0, self._shrink_to_fit)

    # ------------------------------------------------------------- helpers
    @property
    def is_expanded(self) -> bool:
        return self._detail.isVisible()

    def _set_running_style(self, running: bool) -> None:
        self._running = running
        self._toggle_btn.setText("■  Stop" if running else "▶  Start")
        self._toggle_btn.setProperty("running", running)
        # Re-polish so the [running] property selector takes effect.
        self._toggle_btn.style().unpolish(self._toggle_btn)
        self._toggle_btn.style().polish(self._toggle_btn)
        if running:
            self._health_timer.start()
        else:
            self._health_timer.stop()

    def _show_health(self, summary: str, detail: str) -> None:
        """Show the health report: one line always, the probes when expanded.

        The panel's height comes from the text rather than being reserved in advance. It used to be
        a flat 96px — "room for the 4 probe lines" — and increment 5's two extra probes fell off the
        bottom, invisible: a word-wrapped QLabel does not tell a layout how tall it needs to be, so
        a long path silently ate the lines below it. The probe whose whole job is to be read before
        you send a recording was the first to vanish.
        """
        self._health_summary.setText(summary)
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
        self._status_widget.set_status_value(status_value, message)
        # Follow STOPPED too, not just ERROR: the bot can stop itself (e.g. a usage limit halts
        # it), and leaving the button on "Stop" would force a dead click before Start works.
        if status_value in (BotStatus.ERROR.value, BotStatus.STOPPED.value):
            self._set_running_style(False)

    def _on_error(self, message: str) -> None:
        self._append_log(f"⚠️  {message}")
        self._status_widget.set_status(BotStatus.ERROR, message)
        self._set_running_style(False)

    def _append_log(self, line: str) -> None:
        self._log_view.appendPlainText(line)

    # ------------------------------------------------------------- lifecycle
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self._health_timer.stop()
        self._worker.shutdown()
        super().closeEvent(event)
