"""校验 Telegram 凭证：api_id / api_hash 是否被 Telegram 接受、bot token 是否有效。

面板「系统设置 → Telegram 凭证」的测试按钮用这里。
原则：只做无副作用探测（getMe、help.getConfig），不触发登录码、不发消息。
"""

from __future__ import annotations

import asyncio
import logging
import re

from telegram_bot.shared import netproxy
from telegram_bot.shared.botapi import get_me

logger = logging.getLogger(__name__)

_API_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_BOT_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


def check_formats(api_id: object, api_hash: str, bot_token: str) -> list[dict[str, object]]:
    """纯本地格式检查，不发网络请求。"""
    checks: list[dict[str, object]] = []
    raw_id = str(api_id or "").strip()
    if not raw_id:
        checks.append({"name": "api_id 格式", "ok": False, "detail": "未填写"})
    elif not raw_id.isdigit() or int(raw_id) <= 0:
        checks.append({"name": "api_id 格式", "ok": False, "detail": "必须是纯数字"})
    else:
        checks.append({"name": "api_id 格式", "ok": True, "detail": raw_id})
    hash_value = (api_hash or "").strip()
    if not hash_value:
        checks.append({"name": "api_hash 格式", "ok": False, "detail": "未填写"})
    elif not _API_HASH_RE.match(hash_value):
        checks.append({"name": "api_hash 格式", "ok": False, "detail": "应为 32 位十六进制"})
    else:
        checks.append({"name": "api_hash 格式", "ok": True, "detail": "已填写，32 位十六进制"})
    token = (bot_token or "").strip()
    if not token:
        checks.append({"name": "bot token 格式", "ok": False, "detail": "未填写"})
    elif not _BOT_TOKEN_RE.match(token):
        checks.append({"name": "bot token 格式", "ok": False, "detail": "形如 123456789:AA…（数字:字符串）"})
    else:
        checks.append({"name": "bot token 格式", "ok": True, "detail": token.split(":", 1)[0]})
    return checks


def check_bot_token(bot_token: str, *, timeout: float = 15.0) -> dict[str, object]:
    me = get_me(bot_token, timeout=timeout)
    if me.get("ok"):
        username = me.get("username") or ""
        detail = f"@{username}" if username else str(me.get("name") or "已通过")
        return {"name": "bot token 有效", "ok": True, "detail": detail}
    return {"name": "bot token 有效", "ok": False, "detail": str(me.get("error") or "校验失败")}


async def _probe_api(api_id: int, api_hash: str) -> None:
    from telethon import TelegramClient
    from telethon.sessions import MemorySession
    from telethon.tl import functions

    client = TelegramClient(
        MemorySession(),
        api_id,
        api_hash,
        connection_retries=1,
        request_retries=1,
        timeout=10,
        proxy=netproxy.telethon_proxy(),
    )
    await client.connect()
    try:
        # help.getConfig 会带上 api_id 发到服务端，无效时抛 ApiIdInvalidError。
        await client(functions.help.GetConfigRequest())
    finally:
        await client.disconnect()


def check_api_credentials(api_id: object, api_hash: str, *, timeout: float = 20.0) -> dict[str, object]:
    """真实连一次 Telegram 校验 api_id/api_hash，不写会话文件、不触发验证码。"""
    raw_id = str(api_id or "").strip()
    if not raw_id.isdigit() or not (api_hash or "").strip():
        return {"name": "api_id / api_hash 可用", "ok": False, "detail": "未填写或格式不对"}
    try:
        from telethon.errors import ApiIdInvalidError
    except ImportError:
        return {"name": "api_id / api_hash 可用", "ok": False, "detail": "运行环境没有 telethon，无法校验"}

    async def _run() -> None:
        await asyncio.wait_for(_probe_api(int(raw_id), api_hash.strip()), timeout)

    try:
        asyncio.run(_run())
    except ApiIdInvalidError:
        return {"name": "api_id / api_hash 可用", "ok": False, "detail": "Telegram 拒绝这对凭证（API_ID_INVALID）"}
    except asyncio.TimeoutError:
        return {"name": "api_id / api_hash 可用", "ok": False, "detail": f"连接超时（>{timeout:.0f} 秒），检查网络或代理"}
    except Exception as exc:  # noqa: BLE001 - 测试按钮只做提示
        detail = f"{exc.__class__.__name__}: {exc}"
        logger.warning("校验 api_id/api_hash 失败: %s", detail)
        return {"name": "api_id / api_hash 可用", "ok": False, "detail": detail[:160]}
    return {"name": "api_id / api_hash 可用", "ok": True, "detail": "Telegram 已接受"}
