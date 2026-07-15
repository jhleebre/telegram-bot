from dataclasses import replace

from contextbot.core.health import HealthChecker, HealthStatus

from .conftest import FakeBot, FakeClient


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


async def test_report_as_text(settings):
    client = FakeClient(authorized=True)
    await client.connect()
    text = (await HealthChecker(client, FakeBot(), settings).check()).as_text()
    for name in ("telethon-auth", "bot-token", "inbox", "connection"):
        assert name in text


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
