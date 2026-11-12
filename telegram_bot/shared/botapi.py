"""通过 Bot API 直接发通知（不依赖用户会话，网络抖动时更可靠）。"""

from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


def send_bot_message(
    token: str,
    chat_id: str,
    text: str,
    *,
    timeout: float = 20.0,
) -> bool:
    """同步发送一条纯文本消息，失败只记日志、返回 False，不抛异常。"""
    token = (token or "").strip()
    chat_id = (chat_id or "").strip()
    if not token or not chat_id or not text:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, data={"chat_id": chat_id, "text": text})
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if resp.status_code != 200 or not payload.get("ok"):
            logger.warning(
                "Bot API 通知发送失败: status=%s body=%s",
                resp.status_code,
                (resp.text or "")[:200],
            )
            return False
        return True
    except Exception:  # noqa: BLE001 - 通知失败不能影响主流程
        logger.warning("Bot API 通知发送异常", exc_info=True)
        return False


async def send_bot_message_async(
    token: str,
    chat_id: str,
    text: str,
    *,
    timeout: float = 20.0,
) -> bool:
    return await asyncio.to_thread(send_bot_message, token, chat_id, text, timeout=timeout)
