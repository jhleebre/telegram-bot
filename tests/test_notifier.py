from contextbot.core.notifier import Notifier

from .conftest import FakeBot


async def test_send_to_owner():
    bot = FakeBot()
    notifier = Notifier(bot, owner_chat_id=42)
    ok = await notifier.send("hello")
    assert ok is True
    assert bot.sent == [(42, "hello")]


async def test_send_without_owner_drops():
    bot = FakeBot()
    notifier = Notifier(bot, owner_chat_id=None)
    ok = await notifier.send("hello")
    assert ok is False
    assert bot.sent == []


async def test_set_owner_chat_id():
    bot = FakeBot()
    notifier = Notifier(bot)
    notifier.set_owner_chat_id(7)
    assert notifier.owner_chat_id == 7
    await notifier.send("hi")
    assert bot.sent == [(7, "hi")]


async def test_send_failure_is_swallowed():
    class BoomBot:
        async def send_message(self, chat_id, text):
            raise RuntimeError("network down")

    notifier = Notifier(BoomBot(), owner_chat_id=1)
    assert await notifier.send("x") is False
