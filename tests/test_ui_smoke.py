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


def test_the_health_panel_shows_every_probe_it_is_given(qtbot):
    """A probe you cannot read is the failure it exists to prevent.

    The panel reserved a flat 96px — "room for the 4 probe lines" — and the window declared a fixed
    minimum *height* of 620. Increment 5's two extra probes (whisper-stt, glossary) pushed the
    content past both, so Qt squeezed the cards below their own minimums and clipped the bottom of
    the health card: the two new probes were invisible and claude-engine was cut off mid-path.

    That is not cosmetic. The whisper-stt probe's whole job is to say "model not downloaded"
    **before** the owner sends a recording — and its message is the longest line the panel ever
    shows, so it was the first to disappear. Exercised at the narrowest allowed width, which is the
    worst case for wrapping.
    """
    from contextbot.core.health import HealthReport, HealthStatus, ProbeResult

    worker = StubWorker()
    window = MainWindow(worker)
    qtbot.addWidget(window)
    report = HealthReport(
        overall=HealthStatus.DEGRADED,
        probes=[
            ProbeResult("telethon-auth", True, "authorized"),
            ProbeResult("bot-token", True, "@my_context_bot"),
            ProbeResult("inbox", True, "/Users/1111068/Documents/MarkNotes/0_inbox"),
            ProbeResult("connection", True, "connected"),
            ProbeResult("claude-engine", True, "/opt/homebrew/bin/claude (sonnet)"),
            ProbeResult("whisper-stt", False, "model not downloaded (약 1.5GB) — run scripts/download_model.py"),
            ProbeResult("glossary", True, "glossary.md (19KB) — /Users/1111068/Documents/MarkNotes/.claude/contextbot"),
        ],
    )
    window.show()
    worker.health_ready.emit(report.as_text())

    # Shrink it the way dragging a corner would. It must refuse to go below what it has to show.
    window.resize(window.minimumWidth(), 300)

    label = window._health_label
    needed = label.heightForWidth(label.width())
    assert needed <= label.height(), (
        f"the health panel is clipped: needs {needed}px, has {label.height()}px — "
        "some probes are invisible"
    )


def test_the_window_has_no_fixed_minimum_height(qtbot):
    """The layout owns its own floor.

    A hardcoded minimum height is a promise about how much content there is, and adding a probe
    breaks it silently — which is exactly how the panel above came to be clipped. Letting
    minimumSizeHint be the floor means the next probe cannot reintroduce it.
    """
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)

    assert window.minimumHeight() == 0, "a fixed minimum height lets the window clip its own content"
    assert window.minimumWidth() > 0, "width still needs a floor — the text is monospaced paths"
