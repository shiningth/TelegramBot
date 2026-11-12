"""一次性脚本：为 LTX 采集登录独立的用户会话（ltx_user.session）。

在容器里运行（交互式）：
    docker exec -it tgbot python /tmp/telegram_user_login.py          # 验证码发到 Telegram App
    docker exec -it tgbot python /tmp/telegram_user_login.py --sms    # 强制走短信
    docker exec -it tgbot python /tmp/telegram_user_login.py --qr     # 二维码链接（需较新客户端）

脚本只在本机创建会话文件，不打印、不上传任何验证码或密码。
"""
from __future__ import annotations

import asyncio
import getpass
import json
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

CONFIG = Path("/data/config/config.json")
SESSION = Path("/app/sessions/ltx_user")
CHANNEL = "examplechan"

_TYPE_HINT = {
    "SentCodeTypeApp": "已发送到你的 Telegram App —— 请打开与「Telegram」官方账号的对话查看（不是短信）",
    "SentCodeTypeSms": "已通过短信发送到你的手机号",
    "SentCodeTypeCall": "稍后会有语音电话播报验证码",
    "SentCodeTypeFlashCall": "会有一个来电，号码即验证码",
    "SentCodeTypeMissedCall": "会响一声，来电号码后几位即验证码",
    "SentCodeTypeFragmentSms": "通过 Fragment 短信发送",
}


def _describe(sent) -> None:
    kind = type(getattr(sent, "type", None)).__name__
    print(f"发送方式：{_TYPE_HINT.get(kind, kind)}")
    timeout = getattr(sent, "timeout", None)
    if timeout:
        print(f"注意：{timeout} 秒内不能再次请求，太频繁会被临时限制。")


async def _send_code(client: TelegramClient, phone: str, force_sms: bool) -> object:
    try:
        return await client.send_code_request(phone, force_sms=force_sms)
    except FloodWaitError as exc:
        raise SystemExit(f"请求太频繁，Telegram 要求等待 {exc.seconds} 秒后再试")


async def _qr_login(client: TelegramClient) -> None:
    qr = await client.qr_login()
    print("\n用手机 Telegram 打开下面的链接并确认（约 2 分钟有效）：\n")
    print(qr.url, "\n")
    while True:
        try:
            await qr.wait(timeout=120)
            return
        except asyncio.TimeoutError:
            qr = await client.qr_login()
            print("链接过期，换成新的：\n")
            print(qr.url, "\n")


async def _phone_login(client: TelegramClient, force_sms: bool) -> None:
    phone = input("手机号（含国家码，例如 +8613800138000）：").strip().replace(" ", "")
    while not phone:
        phone = input("手机号不能为空，请重新输入：").strip()
    sent = await _send_code(client, phone, force_sms)
    _describe(sent)
    for attempt in range(3):
        code = input("请输入验证码：").strip().replace(" ", "")
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
            return
        except SessionPasswordNeededError:
            password = getpass.getpass("该账号开启了两步验证，请输入密码（不回显）：")
            await client.sign_in(password=password)
            return
        except PhoneCodeInvalidError:
            print(f"验证码不正确（第 {attempt + 1} 次）")
        except PhoneCodeExpiredError:
            print("验证码已过期，重新发送…")
            sent = await _send_code(client, phone, force_sms)
            _describe(sent)
    raise SystemExit("验证码多次失败，请重新运行脚本")


async def main() -> None:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))["telegram"]
    client = TelegramClient(str(SESSION), cfg["api_id"], cfg["api_hash"])
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"这个会话已经登录过了：{me.id} @{me.username}")
    else:
        if "--qr" in sys.argv:
            await _qr_login(client)
        else:
            await _phone_login(client, force_sms="--sms" in sys.argv)
        me = await client.get_me()
        print(f"\n登录成功：{me.id} @{me.username} {me.first_name or ''}")

    try:
        entity = await client.get_entity(CHANNEL)
        msgs = await client.get_messages(entity, limit=1)
        print(f"频道可读：{getattr(entity, 'title', CHANNEL)}")
        if msgs:
            print(f"最新一条消息 id={msgs[0].id} 时间={msgs[0].date}")
            print(f"预览：{(msgs[0].message or '')[:60]}")
    except Exception as exc:  # noqa: BLE001
        print(f"读取频道失败：{type(exc).__name__}: {exc}")

    await client.disconnect()
    print(f"\n会话文件：{SESSION}.session")


if __name__ == "__main__":
    asyncio.run(main())