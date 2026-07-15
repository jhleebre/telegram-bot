"""GUI smoke tests. Skipped automatically when PySide6/pytest-qt/display are unavailable."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

# Use the offscreen platform so these run headlessly (CI, no display).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402

from contextbot.core.status import STATUS_COLORS, BotStatus  # noqa: E402
from contextbot.ui.main_window import MainWindow  # noqa: E402
from contextbot.ui.status_widget import StatusWidget  # noqa: E402


def test_status_widget_reflects_status(qtbot):
    widget = StatusWidget()
    qtbot.addWidget(widget)

    widget.set_status(BotStatus.RUNNING, "실행 중")
    assert "Running" in widget._text.text()
    assert STATUS_COLORS[BotStatus.RUNNING] in widget._light.styleSheet()

    widget.set_status_value(BotStatus.ERROR.value, "boom")
    assert "Error" in widget._text.text()
    assert STATUS_COLORS[BotStatus.ERROR] in widget._light.styleSheet()


class StubWorker(QObject):
    status_changed = Signal(str, str)
    log_line = Signal(str)
    health_ready = Signal(str)
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self.started = False
        self.stopped = False
        self.health_requests = 0

    def start_bot(self):
        self.started = True

    def stop_bot(self):
        self.stopped = True

    def request_health(self):
        self.health_requests += 1

    def shutdown(self, timeout: float = 10.0):
        pass


def test_main_window_toggle_and_signals(qtbot):
    worker = StubWorker()
    window = MainWindow(worker)
    qtbot.addWidget(window)

    assert "Start" in window._toggle_btn.text()
    assert window._toggle_btn.property("running") is False

    # Start
    window._on_toggle()
    assert worker.started is True
    assert "Stop" in window._toggle_btn.text()
    assert window._toggle_btn.property("running") is True
    assert worker.health_requests >= 1

    # Status signal updates the widget.
    worker.status_changed.emit(BotStatus.RUNNING.value, "실행 중")
    assert "Running" in window._status_widget._text.text()

    # Stop
    window._on_toggle()
    assert worker.stopped is True
    assert "Start" in window._toggle_btn.text()

    # Log + health signals.
    worker.log_line.emit("hello log")
    assert "hello log" in window._log_view.toPlainText()
    worker.health_ready.emit("🟢 Health: healthy")
    assert "healthy" in window._health_label.text()


def test_error_signal_resets_button(qtbot):
    worker = StubWorker()
    window = MainWindow(worker)
    qtbot.addWidget(window)
    window._on_toggle()  # start
    worker.error.emit("bad token")
    assert "Start" in window._toggle_btn.text()
    assert "bad token" in window._log_view.toPlainText()
