from dataclasses import replace

import pytest

from contextbot.core.health import HealthChecker, HealthStatus
from contextbot.stt import whisper

from .conftest import FakeBot, FakeClient


@pytest.fixture(autouse=True)
def stt_present(monkeypatch):
    """Pin the STT probe healthy unless a test says otherwise.

    Without this the suite would assert against *this machine's* HuggingFace cache — a green
    `test_healthy` would mean "the owner happens to have 1.5GB of weights", and the suite would go
    red on any machine that does not. The probe's own states are tested below, deliberately.
    """
    monkeypatch.setattr(whisper, "package_is_installed", lambda: True)
    monkeypatch.setattr(whisper, "model_is_cached", lambda model: True)
    monkeypatch.setattr(whisper, "resolve_ffmpeg", lambda executable="ffmpeg": "/opt/homebrew/bin/ffmpeg")


async def test_healthy(settings):
    client = FakeClient(authorized=True)
    await client.connect()
    report = await HealthChecker(client, FakeBot(username="mybot"), settings).check()
    assert report.overall == HealthStatus.HEALTHY
    assert report.probe("telethon-auth").ok
    assert report.probe("bot-token").detail == "@mybot"
    assert report.probe("inbox").ok
    assert report.probe("connection").ok


async def test_unauthorized_is_error(settings):
    client = FakeClient(authorized=False)
    await client.connect()
    report = await HealthChecker(client, FakeBot(), settings).check()
    assert report.overall == HealthStatus.ERROR
    assert not report.probe("telethon-auth").ok


async def test_bad_bot_token_is_error(settings):
    client = FakeClient(authorized=True)
    await client.connect()
    report = await HealthChecker(client, FakeBot(fail=True), settings).check()
    assert report.overall == HealthStatus.ERROR
    assert not report.probe("bot-token").ok


async def test_missing_inbox_is_degraded(settings, tmp_path):
    client = FakeClient(authorized=True)
    await client.connect()
    bad = replace(settings, inbox_dir=tmp_path / "does_not_exist")
    report = await HealthChecker(client, FakeBot(), bad).check()
    assert report.overall == HealthStatus.DEGRADED
    assert not report.probe("inbox").ok


async def test_disconnected_reported_but_not_fatal(settings):
    client = FakeClient(authorized=True)  # not connected
    report = await HealthChecker(client, FakeBot(), settings).check()
    assert report.overall == HealthStatus.HEALTHY
    assert report.probe("connection").ok is False


async def test_report_as_html(settings):
    client = FakeClient(authorized=True)
    await client.connect()
    html = (await HealthChecker(client, FakeBot(), settings).check()).as_html()
    for name in ("telethon-auth", "bot-token", "inbox", "connection"):
        assert name in html


# ------------------------------------------------- claude-engine probe
async def test_claude_probe_disabled_is_ok(settings):
    """CLAUDE_ENABLED=false is a deliberate choice, not a fault."""
    client = FakeClient(authorized=True)
    await client.connect()
    report = await HealthChecker(client, FakeBot(), settings).check()

    probe = report.probe("claude-engine")
    assert probe.ok and "disabled" in probe.detail
    assert report.overall == HealthStatus.HEALTHY


async def test_claude_probe_reports_resolved_path(settings, make_claude):
    script = make_claude("pass")
    client = FakeClient(authorized=True)
    await client.connect()
    enabled = replace(settings, claude_enabled=True, claude_bin=str(script), claude_model="haiku")

    report = await HealthChecker(client, FakeBot(), enabled).check()

    probe = report.probe("claude-engine")
    assert probe.ok
    assert str(script) in probe.detail and "haiku" in probe.detail
    assert report.overall == HealthStatus.HEALTHY


async def test_missing_claude_is_degraded_not_error(settings):
    """A missing CLI must stay visible in the UI, but capture still works — so: DEGRADED."""
    client = FakeClient(authorized=True)
    await client.connect()
    enabled = replace(settings, claude_enabled=True, claude_bin="claude-nope-xyz")

    report = await HealthChecker(client, FakeBot(), enabled).check()

    probe = report.probe("claude-engine")
    assert not probe.ok
    assert "not found" in probe.detail
    assert report.overall == HealthStatus.DEGRADED


def _auth_script(logged_in) -> str:
    """A fake `claude` that answers `auth status --json`, else exits 0 silently."""
    payload = "None" if logged_in is None else repr({"loggedIn": logged_in})
    return (
        'if "auth" in sys.argv[1:]:\n'
        f"    payload = {payload}\n"
        "    if payload is not None:\n"
        "        print(json.dumps(payload))\n"
    )


async def test_logged_out_claude_is_degraded(settings, make_claude):
    """The gap the first PDF run exposed: a resolvable but signed-out CLI was reported green."""
    script = make_claude(_auth_script(False))
    client = FakeClient(authorized=True)
    await client.connect()
    enabled = replace(settings, claude_enabled=True, claude_bin=str(script))

    report = await HealthChecker(client, FakeBot(), enabled).check()

    probe = report.probe("claude-engine")
    assert not probe.ok
    assert "logged out" in probe.detail
    assert report.overall == HealthStatus.DEGRADED


async def test_logged_in_claude_is_healthy(settings, make_claude):
    script = make_claude(_auth_script(True))
    client = FakeClient(authorized=True)
    await client.connect()
    enabled = replace(settings, claude_enabled=True, claude_bin=str(script), claude_model="haiku")

    report = await HealthChecker(client, FakeBot(), enabled).check()

    probe = report.probe("claude-engine")
    assert probe.ok
    assert str(script) in probe.detail and "haiku" in probe.detail
    assert report.overall == HealthStatus.HEALTHY


async def test_undeterminable_auth_does_not_cry_wolf(settings, make_claude):
    """When `auth status` gives nothing parseable, stay green — the binary is at least present."""
    script = make_claude(_auth_script(None))
    client = FakeClient(authorized=True)
    await client.connect()
    enabled = replace(settings, claude_enabled=True, claude_bin=str(script))

    report = await HealthChecker(client, FakeBot(), enabled).check()

    assert report.probe("claude-engine").ok
    assert report.overall == HealthStatus.HEALTHY


# ------------------------------------------------- whisper-stt probe (increment 5)
async def _report(settings):
    client = FakeClient(authorized=True)
    await client.connect()
    return await HealthChecker(client, FakeBot(), settings).check()


async def test_stt_probe_reports_the_model_and_the_resolved_ffmpeg(settings):
    """The ffmpeg *path* is the point, not just that it exists: a Dock launch resolves a different
    one, or none (the increment-1 bug, third appearance)."""
    report = await _report(settings)

    probe = report.probe("whisper-stt")
    assert probe.ok
    assert "whisper-large-v3-turbo" in probe.detail  # the name, not the whole repo id
    assert "/opt/homebrew/bin/ffmpeg" in probe.detail


async def test_an_undownloaded_model_is_degraded_not_error(settings, monkeypatch):
    """The whole reason this probe exists.

    The weights are an HF repo id, not a package dependency, so `pip install` never brings them and
    nobody installs them: mlx_whisper fetches 1.5GB *inside* the first job that needs it — minutes
    of unexplained stall, or an outright failure offline. DEGRADED, never ERROR: every other
    pipeline still works, so this must not stop the bot.
    """
    monkeypatch.setattr(whisper, "model_is_cached", lambda model: False)

    report = await _report(settings)

    assert not report.probe("whisper-stt").ok
    assert "download_model.py" in report.probe("whisper-stt").detail
    assert report.overall == HealthStatus.DEGRADED


async def test_a_missing_package_is_degraded(settings, monkeypatch):
    monkeypatch.setattr(whisper, "package_is_installed", lambda: False)

    report = await _report(settings)

    assert not report.probe("whisper-stt").ok
    assert "mlx-whisper" in report.probe("whisper-stt").detail
    assert report.overall == HealthStatus.DEGRADED


async def test_a_missing_ffmpeg_is_degraded(settings, monkeypatch):
    """The increment-1 PATH bug's third appearance: /opt/homebrew/bin is invisible from the Dock."""
    monkeypatch.setattr(whisper, "resolve_ffmpeg", lambda executable="ffmpeg": None)

    report = await _report(settings)

    assert not report.probe("whisper-stt").ok
    assert "ffmpeg" in report.probe("whisper-stt").detail
    assert report.overall == HealthStatus.DEGRADED


async def test_a_missing_glossary_is_reported_but_not_a_failure(settings):
    """Absent is a valid, quiet state — but an invisible one, and a glossary at the wrong path
    means every meeting note silently loses its term corrections."""
    report = await _report(settings)

    probe = report.probe("glossary")
    assert probe.ok
    assert "none at" in probe.detail
    assert report.overall == HealthStatus.HEALTHY


async def test_a_present_glossary_is_reported(settings):
    settings.glossary_path.write_text("| 팀웹 | T-map | 제품명 | |\n", encoding="utf-8")

    probe = (await _report(settings)).probe("glossary")

    assert probe.ok
    assert settings.glossary_path.name in probe.detail
    assert str(settings.glossary_path.parent) in probe.detail


# ------------------------------------------- the panel speaks the bar's language
def test_the_panel_uses_the_same_coloured_dot_as_the_bar(settings):
    """The ✅/❌/🟡 emoji were a second visual vocabulary a few pixels from the bar's light: louder,
    a different shape, and a different palette, all saying what the light says quietly."""
    from contextbot.core.health import HEALTH_COLORS, HealthReport, HealthStatus, ProbeResult

    html = HealthReport(
        overall=HealthStatus.DEGRADED,
        probes=[ProbeResult("inbox", True, "/x"), ProbeResult("whisper-stt", False, "no model")],
    ).as_html()

    for emoji in ("✅", "❌", "🟡", "🟢", "🔴"):
        assert emoji not in html, f"{emoji} is the old vocabulary"
    assert HEALTH_COLORS[HealthStatus.DEGRADED] in html   # the header carries the severity…
    assert HEALTH_COLORS[HealthStatus.HEALTHY] in html    # …and each probe its own answer
    assert HEALTH_COLORS[HealthStatus.ERROR] in html


def test_the_panel_gives_its_lines_room_to_read(settings):
    """Seven probes are a list to be scanned, not a paragraph. At the font's default leading the
    marks in the left column nearly touched."""
    from contextbot.core.health import HealthReport, HealthStatus, ProbeResult

    html = HealthReport(overall=HealthStatus.HEALTHY, probes=[ProbeResult("inbox", True)]).as_html()

    assert "line-height:150%" in html
    assert "padding-bottom:5px" in html


def test_a_wrapped_detail_hangs_under_the_text_not_under_the_dot(settings):
    """Laid out as one flowing paragraph, a detail long enough to wrap put its continuation back
    under the dot — a line with no mark, in the column reserved for marks, which reads as a probe
    that lost its answer. Two table columns give the hanging indent for free; Qt's rich text ignores
    the negative `text-indent` that would be the usual way to ask."""
    from contextbot.core.health import HealthReport, HealthStatus, ProbeResult

    html = HealthReport(
        overall=HealthStatus.HEALTHY,
        probes=[ProbeResult("whisper-stt", True, "a detail long enough to wrap " * 5)],
    ).as_html()

    assert html.startswith("<table")
    assert html.count("<td") == 2 * 2, "one dot cell and one text cell per row (header + probe)"


def test_a_detail_containing_markup_cannot_break_the_panel(settings):
    """The panel is rich text now, and a probe's detail is a path or an error message — neither of
    which promises to be free of `<`. Unescaped, one would eat the rest of the report."""
    from contextbot.core.health import HealthReport, HealthStatus, ProbeResult

    html = HealthReport(
        overall=HealthStatus.ERROR,
        probes=[ProbeResult("inbox", False, "not writable: <Errno 13> & denied")],
    ).as_html()

    assert "&lt;Errno 13&gt;" in html
    assert "&amp; denied" in html
