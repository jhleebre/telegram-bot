"""GUI smoke tests. Skipped automatically when PySide6/pytest-qt/display are unavailable.

The window is a status bar: one row, collapsed, that expands on demand. Most of what is asserted
here is *layout*, which is unusual for a test suite and earned — every UI bug in this project so far
has been a constant quietly overriding what the layout already knew, and not one of them raised.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

# Use the offscreen platform so these run headlessly (CI, no display).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QWidget  # noqa: E402

from contextbot.core.health import (  # noqa: E402
    HEALTH_COLORS,
    HEALTH_UNKNOWN_COLOR,
    HealthReport,
    HealthStatus,
    ProbeResult,
)
from contextbot.core.status import STATUS_TAGLINES, BotStatus  # noqa: E402
from contextbot.ui.elided_label import ElidedLabel  # noqa: E402
from contextbot.ui.icon_buttons import ChevronButton, PlayStopButton  # noqa: E402
from contextbot.ui.main_window import MainWindow  # noqa: E402


class StubWorker(QObject):
    status_changed = Signal(str, str)
    log_line = Signal(str)
    health_ready = Signal(str, str, str)   # (overall, summary, detail)
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


def _report(degraded: bool = False) -> HealthReport:
    """A full seven-probe report, the shape the real one has."""
    return HealthReport(
        overall=HealthStatus.DEGRADED if degraded else HealthStatus.HEALTHY,
        probes=[
            ProbeResult("telethon-auth", True, "authorized"),
            ProbeResult("bot-token", True, "@my_context_bot"),
            ProbeResult("inbox", True, "/Users/1111068/Documents/MarkNotes/0_inbox"),
            ProbeResult("connection", True, "connected"),
            ProbeResult("claude-engine", True, "/opt/homebrew/bin/claude (sonnet)"),
            ProbeResult(
                "whisper-stt",
                not degraded,
                "model not downloaded (약 1.5GB) — run scripts/download_model.py"
                if degraded
                else "whisper-large-v3-turbo · ffmpeg /opt/homebrew/bin/ffmpeg",
            ),
            ProbeResult(
                "glossary", True, "glossary.md (19KB) — /Users/1111068/Documents/MarkNotes/.claude"
            ),
        ],
    )


# --------------------------------------------------------------- the row's text
def test_the_row_shows_the_live_message(qtbot):
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)

    window._on_status_changed(BotStatus.RUNNING.value, "실행 중")

    assert window._message.full_text() == "실행 중"


def test_the_status_falls_back_to_its_tagline(qtbot):
    """No live message → the row still says something human. It is the only text there now."""
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)

    window._on_status_changed(BotStatus.STOPPED.value, "")

    assert window._message.full_text() == STATUS_TAGLINES[BotStatus.STOPPED]


# ------------------------------------------------------------------- behaviour
def test_main_window_toggle_and_signals(qtbot):
    worker = StubWorker()
    window = MainWindow(worker)
    qtbot.addWidget(window)

    assert window._play_btn.is_running is False

    window._on_toggle()
    assert worker.started is True
    assert window._play_btn.is_running is True

    worker.status_changed.emit(BotStatus.RUNNING.value, "실행 중")
    assert window._message.full_text() == "실행 중"

    window._on_toggle()
    assert worker.stopped is True
    assert window._play_btn.is_running is False

    worker.log_line.emit("hello log")
    assert "hello log" in window._log_view.toPlainText()


def test_clicking_start_does_not_ask_for_health_yet(qtbot):
    """`start_bot` only queues the work, so asking now asks a client that has not connected — and
    it does not answer "unknown", it answers `telethon-auth: not logged in (run login.py)`: red,
    alarming, and false on a machine that is logged in fine. The worker asks once the starting
    sequence really finishes; until then the light stays grey, which is the honest answer.
    """
    worker = StubWorker()
    window = MainWindow(worker)
    qtbot.addWidget(window)

    window._on_toggle()

    assert worker.health_requests == 0
    assert HEALTH_UNKNOWN_COLOR in window._health_light.styleSheet()


def test_error_signal_resets_button(qtbot):
    worker = StubWorker()
    window = MainWindow(worker)
    qtbot.addWidget(window)
    window._on_toggle()

    worker.error.emit("bad token")

    assert window._play_btn.is_running is False
    assert "bad token" in window._log_view.toPlainText()


# ------------------------------------------------------------------- the light
def test_the_health_light_is_colour_only_with_no_caption(qtbot):
    """Green needs no caption, and amber/red have more to say than a row has room for. The summary
    is the tooltip; `⌄` has the whole story."""
    worker = StubWorker()
    window = MainWindow(worker)
    qtbot.addWidget(window)
    report = _report(degraded=True)

    worker.health_ready.emit(report.overall.value, report.summary(), report.as_html())

    assert HEALTH_COLORS[HealthStatus.DEGRADED] in window._health_light.styleSheet()
    assert window._health_light.toolTip() == report.summary()
    assert "whisper-stt" in window._health_label.text()   # the detail, behind ⌄


def test_the_light_is_grey_until_the_first_check(qtbot):
    """Before anything has run there is nothing to report — not even "fine"."""
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)

    assert HEALTH_UNKNOWN_COLOR in window._health_light.styleSheet()


def test_the_health_panel_shows_every_probe_it_is_given(qtbot):
    """A probe you cannot read is the failure it exists to prevent.

    The panel reserved a flat 96px — "room for the 4 probe lines" — and increment 5's two extra
    probes pushed past it, so Qt clipped the bottom of the card: `whisper-stt` and `glossary`
    invisible, `claude-engine` cut off mid-path. The whisper-stt probe's whole job is to say "model
    not downloaded" **before** a recording is sent, and its message is the longest line the panel
    ever shows — so it was the first to disappear.
    """
    worker = StubWorker()
    window = MainWindow(worker)
    qtbot.addWidget(window)
    window.show()
    report = _report(degraded=True)
    worker.health_ready.emit(report.overall.value, report.summary(), report.as_html())
    window._on_expand()
    window.resize(window.minimumSizeHint().width(), 300)
    qtbot.wait(50)

    label = window._health_label
    needed = label.heightForWidth(label.width())
    assert needed <= label.height(), (
        f"the health panel is clipped: needs {needed}px, has {label.height()}px"
    )


# --------------------------------------------------------------------- layout
def test_the_window_sets_no_explicit_minimum_size(qtbot):
    """The layout owns its own floor, in both directions.

    A hardcoded minimum is a promise about how much content there is, and it has been wrong twice:
    it does not shrink the window, it lets Qt squeeze the children past their own minimums and clip
    them, silently. 620px hid the health panel's last two probes (the layout wanted 768); 460px hid
    the expand button — the only way to reach the detail that explains a failing light.
    """
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)

    assert window.minimumWidth() == 0, "an explicit minimum width lets the row clip itself"
    assert window.minimumHeight() == 0, "an explicit minimum height lets the panel clip itself"
    assert window.minimumSizeHint().width() >= 400, "…and the layout's own floor is a real window"


def test_the_collapsed_row_fits_at_the_smallest_allowed_width(qtbot):
    """Everything must be on screen at the layout's own minimum — including the expand button."""
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)
    window.show()
    window.resize(window.minimumSizeHint().width(), window.sizeHint().height())
    qtbot.wait(50)

    for name, widget in [
        ("play", window._play_btn),
        ("message", window._message),
        ("light", window._health_light),
        ("expand", window._expand_btn),
    ]:
        right = widget.geometry().x() + widget.geometry().width()
        assert right <= window._bar.width(), (
            f"{name} overflows: ends at {right}, bar is {window._bar.width()}"
        )


def test_the_row_is_padded_symmetrically(qtbot):
    """The owner's eye beat my first measurement, which mixed two coordinate systems and read 16/21
    for what was really 28 above and 9 below.

    Two things made the top loose: the bar was built with `_card("")`, whose empty title label
    padded it for nothing, and the window layout then handed the bar the *hidden* detail panel's
    stretch, so it grew past its own sizeHint and dumped the slack inside itself.
    """
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(50)

    for name, widget in [("play", window._play_btn), ("message", window._message)]:
        box = widget.geometry()   # parent-relative, so y *is* the padding above it
        above, below = box.y(), window._bar.height() - (box.y() + box.height())
        assert above == below, f"{name}: {above}px above, {below}px below"


def test_the_row_does_not_reflow_as_its_contents_change(qtbot):
    """A status bar that rearranges itself while you read it is worse than one that wastes a few
    pixels. Sized to content, the health text shoved the button 51px sideways as it changed."""
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(50)

    positions = set()
    for message, overall, running in [
        ("", HealthStatus.HEALTHY, False),
        ("회의록 초안 작성 중…", HealthStatus.DEGRADED, True),
        ("아주 " * 40, HealthStatus.ERROR, True),
    ]:
        window._on_status_changed(BotStatus.RUNNING.value, message)
        window._show_health(overall.value, "요약", "자세히")
        window._set_running_style(running)
        qtbot.wait(20)
        positions.add(
            (window._play_btn.x(), window._play_btn.width(),
             window._health_light.x(), window._expand_btn.x())
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


def test_nothing_in_the_window_animates(qtbot):
    """The status used to be a big emoji face that pulsed its opacity on a loop while the bot was
    active, so the app blinked for as long as it was doing its job. The face is gone, but the claim
    outlived it: motion should mean *something changed*."""
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)
    window._on_status_changed(BotStatus.RUNNING.value, "실행 중")

    for child in window.findChildren(QWidget):
        assert child.graphicsEffect() is None, f"{child.objectName() or child} has an effect"


# ----------------------------------------------------------- collapse / expand
def test_the_window_starts_collapsed(qtbot):
    """Collapsed is the normal state: "is it on, and is anything wrong?" is the normal question."""
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)

    assert window.is_expanded is False
    assert not window._detail.isVisible()


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

    assert window.height() <= collapsed, "collapsing must give the expanded height back"


def test_the_collapsed_window_is_one_row(qtbot):
    """The whole point: normally this is a status bar, not a dashboard."""
    window = MainWindow(StubWorker())
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(50)

    assert window.height() < 130, f"collapsed window is {window.height()}px — that is not one row"


# -------------------------------------------------------------------- elision
def test_a_long_message_elides_rather_than_clipping(qtbot):
    """QLabel clips mid-character, which reads as a rendering bug rather than "there is more"."""
    label = ElidedLabel()
    qtbot.addWidget(label)
    label.resize(200, 30)
    label.show()
    qtbot.wait(20)

    label.setText("아주 " * 40 + "긴 메시지입니다")

    assert label.text() != label.full_text(), "it must not try to show the whole thing"
    assert "…" in label.text(), f"expected an ellipsis, got {label.text()!r}"
    assert label.toolTip() == label.full_text(), "what was cut must stay reachable"


def test_the_full_message_survives_elision(qtbot):
    """The label holds a lopped-off copy; the original must be kept, or a later resize would elide
    the already-elided text and eat it a word at a time."""
    label = ElidedLabel()
    qtbot.addWidget(label)
    label.resize(200, 30)
    label.show()
    qtbot.wait(20)
    label.setText("아주 " * 40 + "긴 메시지입니다")

    label.resize(4000, 30)
    qtbot.wait(20)

    assert label.text().endswith("긴 메시지입니다"), "widening must restore the full text"


# ------------------------------------------------------ drawn, not typed
def test_the_chevron_is_one_shape_turned_over(qtbot):
    """`⌄` and `⌃` are *different characters* (U+2304, U+2303). They look like a pair in a table and
    are not one in a typeface — different weights, sizes and baselines — so the chevron changed
    shape and jumped as the panel opened. Painting it and rotating the painter 180° makes up and
    down the same shape by construction rather than by a font's good intentions.

    Asserted on the pixels, because that is where the bug was: every pixel of the up chevron must be
    the diagonally opposite pixel of the down one. The tolerance is for antialiasing, which Qt's
    rasteriser does not promise to make perfectly symmetric — it is not room for a different glyph.
    """
    button = ChevronButton()
    qtbot.addWidget(button)
    button.show()
    qtbot.wait(20)

    down = button.grab().toImage()
    button.set_pointing_up(True)
    qtbot.wait(20)
    up = button.grab().toImage()

    assert up != down, "the chevron must actually turn over"

    width, height = down.width(), down.height()
    worst = 0
    for y in range(height):
        for x in range(width):
            here = down.pixelColor(x, y)
            opposite = up.pixelColor(width - 1 - x, height - 1 - y)
            worst = max(
                worst,
                abs(here.red() - opposite.red()),
                abs(here.green() - opposite.green()),
                abs(here.blue() - opposite.blue()),
            )
    assert worst <= 24, f"the two directions are not the same shape (worst channel diff {worst})"


def test_the_chevron_and_the_play_button_are_the_same_height(qtbot):
    """They sit in one row: matching heights are what let the layout centre controls against each
    other instead of against two fonts' baselines, which is what looked crooked."""
    qtbot.addWidget(chevron := ChevronButton())
    qtbot.addWidget(play := PlayStopButton())

    assert chevron.height() == play.height()


def test_the_play_button_is_not_the_loudest_thing_in_the_room(qtbot):
    """It was 36px of saturated fill — the biggest element in a window whose actual news is the line
    of text beside it. This is a status bar, not a transport control."""
    play = PlayStopButton()
    qtbot.addWidget(play)

    assert play.width() <= 30
    assert play.width() == play.height(), "round means round"
