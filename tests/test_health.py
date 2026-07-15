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
