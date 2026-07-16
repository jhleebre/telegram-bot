"""GUI smoke tests. Skipped automatically when PySide6/pytest-qt/display are unavailable."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

# Use the offscreen platform so these run headlessly (CI, no display).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402

from contextbot.core.status import (  # noqa: E402
    STATUS_COLORS,
    STATUS_EMOJI,
    STATUS_TAGLINES,
    BotStatus,
)
from contextbot.ui.main_window import MainWindow  # noqa: E402
from contextbot.ui.status_widget import StatusWidget  # noqa: E402


def test_status_widget_reflects_status(qtbot):
    widget = StatusWidget()
    qtbot.addWidget(widget)

    widget.set_status(BotStatus.RUNNING, "실행 중")
    assert widget._message.full_text() == "실행 중"
    assert STATUS_COLORS[BotStatus.RUNNING] in widget._light.styleSheet()
    assert STATUS_EMOJI[BotStatus.RUNNING] == widget._light.text()

    widget.set_status_value(BotStatus.ERROR.value, "boom")
    assert widget._message.full_text() == "boom"
    assert STATUS_COLORS[BotStatus.ERROR] in widget._light.styleSheet()


def test_the_status_falls_back_to_its_tagline(qtbot):
    """No live message → the status still says something human."""
    widget = StatusWidget()
    qtbot.addWidget(widget)

    widget.set_status(BotStatus.STOPPED, "")

    assert widget._message.full_text() == STATUS_TAGLINES[BotStatus.STOPPED]


class StubWorker(QObject):
    status_changed = Signal(str, str)
    log_line = Signal(str)
    health_ready = Signal(str, str)   # (summary, detail)
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
    assert window._status_widget._message.full_text() == "실행 중"

    # Stop
    window._on_toggle()
    assert worker.stopped is True
    assert "Start" in window._toggle_btn.text()

    # Log + health signals.
    worker.log_line.emit("hello log")
    assert "hello log" in window._log_view.toPlainText()
    worker.health_ready.emit("🟢 정상", "🟢 Health: healthy\n✅ inbox — /x")
    assert "healthy" in window._health_label.text()   # the detail, shown when expanded
    assert window._health_summary.text() == "🟢 정상"  # the one line, always shown


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
    worker.health_ready.emit(report.summary(), report.as_text())

    # Shrink it the way dragging a corner would. It must refuse to go below what it has to show.
    window.resize(window.minimumWidth(), 300)

    label = window._health_label
    needed = label.heightForWidth(label.width())
    assert needed <= label.height(), (
        f"the health panel is clipped: needs {needed}px, has {label.height()}px — "
        "some probes are invisible"
    )


def test_the_window_sets_no_explicit_minimum_size(qtbot):
    """The layout owns its own floor, in both directions.

    A hardcoded minimum is a promise about how much content there is, and it has been wrong three
    times: it does not shrink the window, it lets Qt squeeze the children past their own minimums
    and clip them, silently. 620px hid the health panel's last two probes (the layout wanted 768);
    460px hid the expand button and half the health summary (the row wants ~592). The layout knows
    both numbers already, so it must be the one that decides.
    """
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)

    assert window.minimumWidth() == 0, "an explicit minimum width lets the row clip itself"
    assert window.minimumHeight() == 0, "an explicit minimum height lets the panel clip itself"
    # …and the layout's own floor is a real, usable window rather than nothing.
    assert window.minimumSizeHint().width() >= 500


def test_the_collapsed_row_fits_at_the_smallest_allowed_width(qtbot):
    """Everything in the row must be on screen at the layout's own minimum — including the expand
    button, which is the only way to reach the detail that explains a failing summary."""
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)
    window.show()
    window._health_summary.setText("🟡 whisper-stt")
    window.resize(window.minimumSizeHint().width(), window.sizeHint().height())
    qtbot.wait(50)

    bar = window._status_widget.parentWidget()
    for name, widget in [
        ("status", window._status_widget),
        ("button", window._toggle_btn),
        ("health", window._health_summary),
        ("expand", window._expand_btn),
    ]:
        right = widget.geometry().x() + widget.geometry().width()
        assert right <= bar.width(), f"{name} overflows the bar: ends at {right}, bar is {bar.width()}"


# ------------------------------------------------- the collapsed window
def test_the_window_starts_collapsed(qtbot):
    """Collapsed is the normal state: "is it on, and is anything wrong?" is the normal question."""
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)

    assert window.is_expanded is False
    assert not window._detail.isVisible()
    # …and the one line that answers the second half is on screen regardless.
    assert window._health_summary.isVisible() or not window.isVisible()


def test_expanding_shows_the_detail_and_collapsing_hides_it(qtbot):
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)
    window.show()

    window._on_expand()
    assert window.is_expanded is True
    assert window._health_label.isVisible()
    assert window._log_view.isVisible()

    window._on_expand()
    assert window.is_expanded is False
    assert not window._health_label.isVisible()


def test_collapsing_shrinks_the_window_back(qtbot):
    """Qt grows a window to fit new content but never shrinks it back, so without the resize the
    frame would keep its expanded height with one row rattling around in it.

    The waits are not incidental: the resize is deferred through the event loop precisely because a
    synchronous one clamps to the *stale* minimumSizeHint of the layout it is trying to leave.
    """
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(50)
    collapsed = window.height()

    window._on_expand()
    qtbot.wait(50)
    expanded = window.height()
    assert expanded > collapsed + 100, "expanding must actually show something"

    window._on_expand()
    qtbot.wait(50)

    # `<=` rather than `==`: Qt's initial shown size can be a little above the real sizeHint, and
    # the collapse settles onto the hint. Giving back *more* than it took is not a failure.
    assert window.height() <= collapsed, "collapsing must give the expanded height back"


def test_the_collapsed_window_is_one_row(qtbot):
    """The whole point: normally this is a status bar, not a dashboard."""
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(50)

    assert window.height() < 130, f"collapsed window is {window.height()}px — that is not one row"


def test_the_status_dot_does_not_animate(qtbot):
    """It used to pulse on a loop whenever the bot was working, so the app blinked for as long as
    it was doing its job. Motion should mean *something changed*; the colour already says "on"."""
    widget = StatusWidget()
    qtbot.addWidget(widget)

    widget.set_status(BotStatus.RUNNING, "실행 중")

    assert widget._light.graphicsEffect() is None
    assert not hasattr(widget, "_pulse")


def test_a_long_message_elides_rather_than_clipping(qtbot):
    """QLabel clips mid-character, which reads as a rendering bug rather than "there is more"."""
    widget = StatusWidget()
    qtbot.addWidget(widget)
    widget.resize(200, 30)
    widget.show()
    qtbot.wait(20)

    widget.set_status(BotStatus.RUNNING, "아주 " * 40 + "긴 메시지입니다")

    shown = widget._message.text()
    assert shown != widget._message.full_text(), "it must not try to show the whole thing"
    assert "…" in shown, f"expected an ellipsis, got {shown!r}"


def test_the_full_message_survives_elision(qtbot):
    """The label holds a lopped-off copy; the widget must keep the real one, or a later resize
    would elide the already-elided text and eat it a word at a time."""
    widget = StatusWidget()
    qtbot.addWidget(widget)
    widget.resize(200, 30)
    widget.show()
    qtbot.wait(20)
    widget.set_status(BotStatus.RUNNING, "아주 " * 40 + "긴 메시지입니다")

    widget.resize(2000, 30)
    qtbot.wait(20)

    assert widget._message.text().endswith("긴 메시지입니다"), "widening must restore the full text"


def test_the_row_is_padded_symmetrically(qtbot):
    """The owner's eye beat my first measurement, which mixed two coordinate systems and read
    16/21 for what was really 28 above and 9 below.

    Two things made the top loose: the bar was built with `_card("")`, whose empty title label
    padded it for nothing, and the window layout then handed the bar the *hidden* detail panel's
    stretch, so it grew past its own sizeHint and dumped the slack inside itself.
    """
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(50)

    bar = window._status_widget.parentWidget()
    for name, widget in [
        ("status", window._status_widget),
        ("button", window._toggle_btn),
        ("health", window._health_summary),
    ]:
        box = widget.geometry()   # parent-relative, so y *is* the padding above it
        above, below = box.y(), bar.height() - (box.y() + box.height())
        assert above == below, f"{name}: {above}px above, {below}px below"


def test_the_row_does_not_reflow_as_its_contents_change(qtbot):
    """A status bar that rearranges itself while you read it is worse than one that wastes a few
    pixels. Sized to content, `🟢 정상` → `🟡 whisper-stt` shoved the Start button 51px left, and
    Start → Stop twitched it another 2."""
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(50)

    positions = set()
    for message, health, running in [
        ("", "🟢 정상", False),
        ("회의록 초안 작성 중…", "🟡 whisper-stt", True),
        ("아주 " * 40, "🔴 telethon-auth, bot-token, inbox", True),
    ]:
        window._status_widget.set_status(BotStatus.RUNNING, message)
        window._health_summary.setText(health)
        window._set_running_style(running)
        qtbot.wait(20)
        positions.add(
            (window._toggle_btn.x(), window._toggle_btn.width(),
             window._health_summary.x(), window._health_summary.width())
        )

    assert len(positions) == 1, f"the row moved: {positions}"


def test_the_window_opens_at_the_size_its_content_wants(qtbot):
    """`show()` gives a top-level window a default initial height rather than its sizeHint, and
    `adjustSize()` does not undo it — which left dead space under the bar on startup: exactly the
    lopsided padding this row was tightened to remove."""
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(50)

    assert window.height() == window.sizeHint().height()
