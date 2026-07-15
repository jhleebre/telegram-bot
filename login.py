#!/usr/bin/env python3
"""One-time interactive Telegram login for the user (Telethon).

Run this yourself in a terminal once. It logs into YOUR Telegram account (phone number + the code
Telegram sends you, plus your 2FA password if you have one), creates the session file the app
reuses, and baselines the high-water-mark so your existing Saved Messages history is NOT imported
as notes.

    .venv/bin/python login.py

Nothing here is automated by the app — you enter your own phone/code/password interactively.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from telethon import TelegramClient  # noqa: E402

from contextbot.config import ConfigError, Settings  # noqa: E402
from contextbot.core.hwm import HighWaterMark  # noqa: E402


async def _run() -> int:
    try:
        settings = Settings.load()
    except ConfigError as exc:
        print(f"[config error]\n{exc}\n\n.env 파일을 먼저 설정하세요 (.env.example 참고).")
        return 1

    settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(settings.session_path), settings.api_id, settings.api_hash)

    # Telethon prompts interactively for phone, login code, and 2FA password as needed.
    await client.start()

    me = await client.get_me()
    username = f"@{me.username}" if getattr(me, "username", None) else me.id
    print(f"✅ 로그인 성공: {username}")

    # Baseline the high-water-mark to the latest Saved Messages id so history isn't re-imported.
    hwm = HighWaterMark(settings.session_path.parent / "hwm.json")
    latest_id = 0
    async for message in client.iter_messages("me", limit=1):
        latest_id = message.id
        break
    hwm.baseline(latest_id)
    print(f"✅ 기준점(HWM) 설정: 최신 Saved Messages id = {latest_id}")
    print("   이후 Saved Messages에 보내는 메시지부터 노트로 저장됩니다.")

    await client.disconnect()
    print("완료. 이제 `.venv/bin/python run.py` 로 앱을 실행하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
