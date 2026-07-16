"""Main application window: a friendly card-based layout.

Header + big status "face" + a prominent Start/Stop button + health and log cards. Styling is
self-contained QSS so the look is consistent regardless of the system theme.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core.status import BotStatus
from .bot_worker import BotWorker
from .status_widget import StatusWidget

_HEALTH_INTERVAL_MS = 15_000
_MAX_LOG_BLOCKS = 500
# A floor only — the panel's real height is derived from the probes it is showing (_show_health).
_HEALTH_MIN_HEIGHT = 96

_STYLESHEET = """
#root {
    background-color: #f4f6fb;
}
QFrame.card {
    background-color: #ffffff;
    border: 1px solid #e6e9f2;
    border-radius: 16px;
}
QLabel#cardTitle {
    font-size: 12px;
    font-weight: 700;
    color: #9aa0b5;
}
QLabel#tagline {
    font-size: 13px;
    color: #7b8199;
}
QLabel#health {
    font-size: 13px;
    color: #3a3f57;
}
#toggle {
    border: none;
    border-radius: 26px;
    padding: 14px 20px;
    font-size: 16px;
    font-weight: 800;
    color: #ffffff;
    background-color: #2ecc71;
}
#toggle:hover { background-color: #29b765; }
#toggle[running="true"] { background-color: #e74c3c; }
#toggle[running="true"]:hover { background-color: #d1412f; }
QPlainTextEdit#log {
    background-color: #1e2233;
    color: #cdd3ea;
    border: none;
    border-radius: 12px;
    padding: 8px;
}
"""


def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setProperty("class", "card")
    frame.setFrameShape(QFrame.NoFrame)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 12, 16, 14)
    layout.setSpacing(8)
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
        # Width only. A fixed minimum *height* (it was 620) is a promise about how much content
        # there is, and increment 5 broke it by adding two probes: 620 is below what the layout
        # needs, so Qt squeezed the cards past their own minimums and clipped the bottom of the
        # health panel — `whisper-stt` and `glossary` gone, `claude-engine` cut off mid-path.
        # Without it, the layout's own minimumSizeHint is the floor, so the window can never be
        # smaller than the thing it has to show.
        self.setMinimumWidth(420)

        # Status card (big friendly face).
        status_card, status_layout = _card("STATUS")
        self._status_widget = StatusWidget()
        status_layout.addWidget(self._status_widget)

        # Prominent toggle button.
        self._toggle_btn = QPushButton("▶  Start")
        self._toggle_btn.setObjectName("toggle")
        self._toggle_btn.setCursor(Qt.PointingHandCursor)
        self._toggle_btn.setProperty("running", False)
        self._toggle_btn.clicked.connect(self._on_toggle)

        # Health card.
        health_card, health_layout = _card("HEALTH")
        self._health_label = QLabel("아직 확인 전이에요")
        self._health_label.setObjectName("health")
        self._health_label.setFont(QFont("Menlo", 12))
        self._health_label.setWordWrap(True)
        # The panel sizes itself to the probes, rather than to a number someone guessed once.
        # It was a flat 96px — "room for the 4 probe lines" — and increment 5's two new probes
        # (whisper-stt, glossary) fell off the bottom, invisible, while claude-engine was cut off
        # mid-path. That is not cosmetic: the *whole point* of the whisper-stt probe is that the
        # owner sees "model not downloaded" **before** they send a recording, and its message is the
        # longest line the panel ever shows. A probe you cannot read is the failure it exists to
        # prevent. A word-wrapped QLabel does not report its wrapped height to a layout unless
        # height-for-width is enabled, so a long path silently ate the lines below it.
        policy = self._health_label.sizePolicy()
        policy.setVerticalPolicy(QSizePolicy.MinimumExpanding)
        policy.setHeightForWidth(True)
        self._health_label.setSizePolicy(policy)
        self._health_label.setMinimumHeight(_HEALTH_MIN_HEIGHT)
        self._health_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        health_layout.addWidget(self._health_label)

        # Log card.
        log_card, log_layout = _card("ACTIVITY")
        self._log_view = QPlainTextEdit()
        self._log_view.setObjectName("log")
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(_MAX_LOG_BLOCKS)
        self._log_view.setFont(QFont("Menlo", 10))
        self._log_view.setMinimumHeight(120)
        log_layout.addWidget(self._log_view)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(14)
        layout.addWidget(status_card)
        layout.addWidget(self._toggle_btn)
        layout.addWidget(health_card)
        layout.addWidget(log_card, 1)  # the log absorbs resizing; other cards keep their size

        # Wire worker signals.
        worker.status_changed.connect(self._on_status_changed)
        worker.log_line.connect(self._append_log)
        worker.health_ready.connect(self._show_health)
        worker.error.connect(self._on_error)

        # Periodic health polling while running.
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(_HEALTH_INTERVAL_MS)
        self._health_timer.timeout.connect(self._worker.request_health)

    # ------------------------------------------------------------- helpers
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

    def _show_health(self, text: str) -> None:
        """Show the health report, and make the panel tall enough to actually show it.

        The height is taken from the text rather than reserved in advance. It used to be a flat
        96px — "room for the 4 probe lines" — and increment 5's two extra probes simply fell off
        the bottom: `whisper-stt` and `glossary` invisible, `claude-engine` cut off mid-path. A
        word-wrapped QLabel does not tell a layout how tall it needs to be, so a long path silently
        ate the lines below it — and the probe whose entire job is to be *read before you send a
        recording* was the one that vanished.

        Deriving the height means the next probe cannot reintroduce this. A bigger constant would
        only postpone it, which is exactly how the 96 got there.
        """
        label = self._health_label
        label.setText(text)
        width = label.width() or label.sizeHint().width()
        label.setMinimumHeight(max(_HEALTH_MIN_HEIGHT, label.heightForWidth(width)))

    # ------------------------------------------------------------- UI actions
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
