"""面板内登录 Telegram 用户会话（采集/签到用的个人账号）。

只做三件事：发验证码 → 提交验证码（必要时提交两步验证密码）→ 写好 session 文件。
不在这里做任何业务逻辑；调用方（app.py）负责把协程丢到主事件循环上跑。
"""

from __future__ import annotations

import logging
import re
import time
import uuid

from telegram_bot.shared import netproxy
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")

_STAGE_LABELS = {
    "code_sent": "等待验证码",
    "password_needed": "等待两步验证密码",
    "done": "登录完成",
    "failed": "登录失败",
}


def _delivery_hint(sent_type: str) -> str:
    mapping = {
        "SentCodeTypeApp": "已发送到你的 Telegram App：打开和「Telegram」官方账号（头像带盾牌）的对话，顶部就是登录码，不是短信。",
        "SentCodeTypeSms": "已用短信发送，请查看手机短信。",
        "SentCodeTypeCall": "会用电话播报验证码，请注意接听。",
        "SentCodeTypeFlashCall": "会闪呼你的手机号，来电号码里包含验证码。",
        "SentCodeTypeFragmentSms": "验证码由 Fragment 发送，请查看 Fragment 绑定的手机。",
    }
    return mapping.get(sent_type, f"验证码已发送（{sent_type}）。")


def normalize_phone(raw: str) -> str:
    phone = re.sub(r"[\s\-()]", "", str(raw or ""))
    if not phone:
        raise ValueError("请填写手机号（含国家码，例如 +8613800138000）")
    if not phone.startswith("+"):
        if phone.startswith("00"):
            phone = "+" + phone[2:]
        elif phone.isdigit() and len(phone) == 11 and phone.startswith("1"):
            phone = "+86" + phone
        else:
            raise ValueError("手机号要带国家码，例如 +8613800138000")
    if not re.fullmatch(r"\+\d{6,15}", phone):
        raise ValueError("手机号格式不对，例如 +8613800138000")
    return phone


def safe_session_name(raw: str) -> str:
    name = str(raw or "").strip()
    if not name:
        raise ValueError("请填写会话名（例如 ltx_user）")
    if not _NAME_RE.match(name) or ".." in name:
        raise ValueError("会话名只能用字母、数字、下划线、短横线")
    return name


def mask_phone(phone: str) -> str:
    if len(phone) <= 6:
        return phone
    return f"{phone[:4]}****{phone[-2:]}"


class SessionLoginManager:
    """内存里维护若干条「登录中」的会话，成功后落盘为 <sessions_dir>/<name>.session。"""

    def __init__(self, sessions_dir: Path, *, ticket_ttl: float = 600.0) -> None:
        self.sessions_dir = Path(sessions_dir)
        self.ticket_ttl = ticket_ttl
        self._tickets: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ 视图
    def sessions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self.sessions_dir.exists():
            return rows
        for path in sorted(self.sessions_dir.glob("*.session")):
            if "." in path.stem:
                # 登录中的临时文件 <name>.<ticket>.session，还不算已登录会话
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            rows.append(
                {
                    "name": path.stem,
                    "size": stat.st_size,
                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
                }
            )
        return rows

    def state(self) -> dict[str, Any]:
        self._prune()
        pending = [
            {
                "ticket": ticket["ticket"],
                "phone": mask_phone(ticket["phone"]),
                "session_name": ticket["session_name"],
                "stage": ticket["stage"],
                "stage_label": _STAGE_LABELS.get(ticket["stage"], ticket["stage"]),
                "detail": ticket["detail"],
            }
            for ticket in self._tickets.values()
        ]
        return {
            "ok": True,
            "sessions": self.sessions(),
            "sessions_dir": str(self.sessions_dir),
            "pending": pending,
        }

    # ------------------------------------------------------------------ 动作
    async def send_code(self, *, phone: str, session_name: str, api_id: int, api_hash: str) -> dict[str, Any]:
        from telethon import TelegramClient

        if not api_id or not api_hash:
            raise ValueError("请先在上面填好 api_id 与 api_hash（并保存），再来登录用户会话")
        number = normalize_phone(phone)
        name = safe_session_name(session_name)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        # 先写到临时文件，登录成功才改成正式名字：失败时不会留下「看起来已登录」的会话
        ticket = uuid.uuid4().hex[:12]
        tmp_path = self.sessions_dir / f"{name}.{ticket}.session"
        final_path = self.sessions_dir / f"{name}.session"
        client = TelegramClient(str(tmp_path), int(api_id), api_hash, proxy=netproxy.telethon_proxy())
        try:
            await client.connect()
            sent = await client.send_code_request(number)
        except Exception:
            await _safe_disconnect(client)
            _unlink(tmp_path)
            raise
        self._tickets[ticket] = {
            "ticket": ticket,
            "phone": number,
            "session_name": name,
            "stage": "code_sent",
            "detail": _delivery_hint(type(sent.type).__name__),
            "phone_code_hash": sent.phone_code_hash,
            "client": client,
            "tmp_path": tmp_path,
            "final_path": final_path,
            "created_at": time.time(),
        }
        logger.info("用户会话登录：已向 %s 发送验证码（session=%s ticket=%s）", mask_phone(number), name, ticket)
        return {"ok": True, "ticket": ticket, "stage": "code_sent", "detail": self._tickets[ticket]["detail"]}

    async def sign_in(self, *, ticket: str, code: str) -> dict[str, Any]:
        from telethon.errors import SessionPasswordNeededError

        entry = self._require(ticket)
        value = str(code or "").strip().replace(" ", "")
        if not value:
            raise ValueError("请填写验证码")
        client = entry["client"]
        try:
            await client.sign_in(phone=entry["phone"], code=value, phone_code_hash=entry["phone_code_hash"])
        except SessionPasswordNeededError:
            entry["stage"] = "password_needed"
            entry["detail"] = "这个账号开了两步验证，请输入两步验证密码。"
            return {"ok": False, "ticket": ticket, "stage": "password_needed", "detail": entry["detail"]}
        me = await self._finish(entry)
        return {"ok": True, "ticket": ticket, "stage": "done", "detail": f"已登录：{me}"}

    async def check_password(self, *, ticket: str, password: str) -> dict[str, Any]:
        entry = self._require(ticket)
        value = str(password or "")
        if not value:
            raise ValueError("请填写两步验证密码")
        await entry["client"].sign_in(password=value)
        me = await self._finish(entry)
        return {"ok": True, "ticket": ticket, "stage": "done", "detail": f"已登录：{me}"}

    async def cancel(self, *, ticket: str) -> dict[str, Any]:
        entry = self._tickets.pop(str(ticket or "").strip(), None)
        if entry is None:
            return {"ok": True, "detail": "没有这条登录请求"}
        await _safe_disconnect(entry["client"])
        _unlink(entry.get("tmp_path"))
        return {"ok": True, "detail": "已取消"}

    # ------------------------------------------------------------------ 内部
    async def _finish(self, entry: dict[str, Any]) -> str:
        client = entry["client"]
        try:
            me = await client.get_me()
            label = f"@{me.username}" if getattr(me, "username", None) else f"{getattr(me, 'first_name', '') or '账号'} ({me.id})"
            if getattr(me, "username", None):
                label += f" ({me.id})"
        except Exception:  # noqa: BLE001 - 拿不到资料不影响文件已保存
            label = "账号"
        try:
            client.session.save()
        except Exception:  # noqa: BLE001
            logger.debug("保存临时会话失败", exc_info=True)
        await _safe_disconnect(client)
        tmp_path = entry.get("tmp_path")
        final_path = entry.get("final_path")
        if tmp_path is not None and final_path is not None:
            try:
                tmp_path.replace(final_path)
            except OSError:
                logger.exception("会话文件改名失败: %s -> %s", tmp_path, final_path)
                raise
        self._tickets.pop(entry["ticket"], None)
        logger.info("用户会话登录完成：session=%s %s", entry["session_name"], label)
        return label

    def _require(self, ticket: str) -> dict[str, Any]:
        entry = self._tickets.get(str(ticket or "").strip())
        if entry is None:
            raise ValueError("登录请求已过期，请重新发送验证码")
        return entry

    def _prune(self) -> None:
        now = time.time()
        for ticket in [key for key, item in self._tickets.items() if now - item["created_at"] > self.ticket_ttl]:
            entry = self._tickets.pop(ticket)
            logger.info("用户会话登录超时，丢弃 ticket=%s", ticket)
            _close_later(entry["client"])


async def _safe_disconnect(client: Any) -> None:
    try:
        await client.disconnect()
    except Exception:  # noqa: BLE001
        logger.debug("断开临时登录客户端失败", exc_info=True)


def _unlink(path: Path | None) -> None:
    """删掉不受大小限制的会话文件（登录中的临时文件、失败的残留）。"""
    if path is None:
        return
    try:
        if path.exists():
            path.unlink()
    except OSError:
        logger.debug("删除会话文件失败: %s", path, exc_info=True)


def _close_later(client: Any) -> None:
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_safe_disconnect(client))
