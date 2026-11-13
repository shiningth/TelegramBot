"""站点签到：按 Cookie 定时请求 PT 站/论坛的签到页。

和「自动签到」模块的区别：
- 自动签到：给 Telegram 里的 bot/群发一条消息（/qd 之类）。
- 站点签到：直接对网站发起 HTTP 请求，适合 NexusPHP 这类网页签到的 PT 站。

所有站点都在配置里，面板增删站点即时生效，不需要重新打包镜像。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from telegram_bot.config import SiteCheckinConfig, SiteCheckinSiteConfig
from telegram_bot.shared.sitecheckin import SignResult, as_dict, sign_site

from .base import ModuleCard
from .checkin import cron_matches

logger = logging.getLogger(__name__)

_LOG_LIMIT = 400
# 与前端 SITE_STATE_LABELS 保持一致，写全局日志时用中文
STATE_LABELS = {
    "signed": "签到成功",
    "already": "今日已签",
    "login": "登录失效",
    "slider": "滑块未过",
    "captcha": "需要人机验证",
    "failed": "失败",
    "unknown": "未知",
}
_STATE_VERSION = 1
_COUNT_RE = re.compile(r"第\s*(\d+)\s*次")


def _sign_count(detail: str) -> int | None:
    match = _COUNT_RE.search(detail or "")
    return int(match.group(1)) if match else None


def _daily_cron_time(expression: str) -> tuple[int, int] | None:
    """解析「10 8 * * *」这类每天固定时刻，返回 (时, 分)；其他写法返回 None。"""
    parts = str(expression or "").split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts
    if dom != "*" or month != "*" or dow != "*":
        return None
    if not (minute.isdigit() and hour.isdigit()):
        return None
    hour_i, minute_i = int(hour), int(minute)
    if not (0 <= hour_i <= 23 and 0 <= minute_i <= 59):
        return None
    return hour_i, minute_i


def is_due(site: SiteCheckinSiteConfig, now: datetime, last_run: datetime | None) -> bool:
    """判断这个站点现在该不该跑。

    每天固定时刻的写法用「到点之后第一次轮询就跑」，避免轮询间隔正好错过那一分钟；
    复杂 cron 仍按分钟精确匹配。
    """
    fixed = _daily_cron_time(site.cron)
    if fixed is None:
        return cron_matches(site.cron, now)
    due = now.replace(hour=fixed[0], minute=fixed[1], second=0, microsecond=0)
    if now < due:
        return False
    if last_run is None:
        return True
    return last_run.date() < now.date()


class SiteCheckinModule:
    def __init__(self, card: ModuleCard, config: SiteCheckinConfig, project_root: Path | None = None) -> None:
        self.card = card
        self.config = config
        self.project_root = project_root
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"version": _STATE_VERSION, "logs": [], "last": {}}
        self._load_state()
        self._sync_card()

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self.card.status = "running"
        self._task = asyncio.create_task(self._loop())
        logger.info("Site checkin started: %d sites", len(self.config.sites))

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.card.status = "stopped"

    async def handle_message(self, message: Any) -> Any:  # pragma: no cover - 不处理消息
        from telegram_bot.shared.contracts import HandlerResult

        return HandlerResult.ignored(self.card.id)

    # ------------------------------------------------------------------ 配置
    def update_config(self, values: dict[str, Any]) -> None:
        from telegram_bot.config import site_checkin_config_from_dict

        merged = {
            "enabled": self.config.enabled,
            "poll_interval_seconds": self.config.poll_interval_seconds,
            "timeout_seconds": self.config.timeout_seconds,
            "log_keep_days": self.config.log_keep_days,
            "flaresolverr": self.config.flaresolverr,
            "notify_enabled": self.config.notify_enabled,
            "notify_bot_token": self.config.notify_bot_token,
            "notify_chat_id": self.config.notify_chat_id,
            "notify_text": self.config.notify_text,
            "state_store": self.config.state_store,
            "sites": [
                {
                    "id": site.id,
                    "name": site.name,
                    "enabled": site.enabled,
                    "url": site.url,
                    "cookie": site.cookie,
                    "cron": site.cron,
                    "method": site.method,
                    "sign_url": site.sign_url,
                    "post_data": site.post_data,
                    "headers": site.headers,
                    "success_keywords": site.success_keywords,
                    "already_keywords": site.already_keywords,
                    "note": site.note,
                }
                for site in self.config.sites
            ],
        }
        merged.update(values)
        # 面板不回显已保存的 Cookie：前端留空即代表「沿用原值」，这里把旧值补回去
        old_cookies = {site.id: site.cookie for site in self.config.sites}
        incoming = merged.get("sites")
        if isinstance(incoming, list):
            for item in incoming:
                if not isinstance(item, dict):
                    continue
                site_id = str(item.get("id") or "").strip()
                if not str(item.get("cookie") or "").strip() and site_id in old_cookies:
                    item["cookie"] = old_cookies[site_id]
        self.config = site_checkin_config_from_dict(merged)
        self._sync_card()
        self._load_state()

    def _sync_card(self) -> None:
        self.card.config = {
            "enabled": self.config.enabled,
            "poll_interval_seconds": self.config.poll_interval_seconds,
            "timeout_seconds": self.config.timeout_seconds,
            "log_keep_days": self.config.log_keep_days,
            "flaresolverr": self.config.flaresolverr,
            "notify_enabled": self.config.notify_enabled,
            "notify_bot_token": self.config.notify_bot_token,
            "has_notify_bot_token": bool(self.config.notify_bot_token),
            "notify_chat_id": self.config.notify_chat_id,
            "notify_text": self.config.notify_text,
            "site_count": len(self.config.sites),
            "enabled_site_count": len([site for site in self.config.sites if site.enabled]),
            "sites": [
                {
                    "id": site.id,
                    "name": site.name,
                    "enabled": site.enabled,
                    "url": site.url,
                    "has_cookie": bool(site.cookie),
                    "cron": site.cron,
                    "method": site.method,
                    "sign_url": site.sign_url,
                    "post_data": site.post_data,
                    "headers": site.headers,
                    "success_keywords": site.success_keywords,
                    "already_keywords": site.already_keywords,
                    "note": site.note,
                    "last": self._state.get("last", {}).get(site.id),
                }
                for site in self.config.sites
            ],
            "logs": list(self._state.get("logs", []))[:60],
        }

    # ------------------------------------------------------------------ 状态
    def _resolve(self, value: str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        base = self.project_root or Path(".")
        return base / path

    def _load_state(self) -> None:
        path = self._resolve(self.config.state_store)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._state = {
                    "version": _STATE_VERSION,
                    "logs": data.get("logs") if isinstance(data.get("logs"), list) else [],
                    "last": data.get("last") if isinstance(data.get("last"), dict) else {},
                }
        except FileNotFoundError:
            self._state = {"version": _STATE_VERSION, "logs": [], "last": {}}
        except Exception:  # noqa: BLE001
            logger.warning("站点签到状态文件读取失败，使用空状态", exc_info=True)
            self._state = {"version": _STATE_VERSION, "logs": [], "last": {}}
        self._prune_logs()

    def _prune_logs(self) -> None:
        keep_days = max(1, int(self.config.log_keep_days or 30))
        cutoff = datetime.now() - timedelta(days=keep_days)
        kept: list[dict[str, Any]] = []
        for entry in self._state.get("logs", []):
            try:
                at = datetime.fromisoformat(str(entry.get("at") or ""))
            except Exception:  # noqa: BLE001
                continue
            if at >= cutoff:
                kept.append(entry)
        self._state["logs"] = kept[-_LOG_LIMIT:]

    def _save_state(self) -> None:
        with self._lock:
            self._prune_logs()
            path = self._resolve(self.config.state_store)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)

    def _refine_already(
        self, site: SiteCheckinSiteConfig, result: SignResult
    ) -> SignResult:
        """签到页永远显示「上一次签到结果」，靠次数与日历标记判断今天签没签。"""
        if result.state != "signed":
            return result
        count = _sign_count(result.detail)
        today = datetime.now().strftime("%Y-%m-%d")
        dates = set(result.dates or ())
        marked = bool(dates) and today in dates
        if dates and not marked:
            # 页面说成功、日历里却没有今天 → 大概率只是回显上一次结果
            result.state = "unknown"
            result.detail = "页面未见今日签到记录（可能未生效）"
            return result
        if count is None:
            return result
        previous = (self._state.get("last") or {}).get(site.id) or {}
        same_day = str(previous.get("at") or "")[:10] == today
        if same_day and previous.get("count") == count:
            result.state = "already"
            tail = " · 日历已标记今日" if marked else ""
            result.detail = f"今日已签（第 {count} 次{tail}）"
        return result

    def _record(self, site: SiteCheckinSiteConfig, result: SignResult) -> dict[str, Any]:
        now = datetime.now()
        entry = {
            "at": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "time": now.strftime("%m-%d %H:%M"),
            "site": site.id,
            "name": site.name or site.id,
            "state": result.state,
            "detail": result.detail[:64],  # 日志只留一句，完整页面另存 debug_html
            "http": result.http,
            "ms": result.elapsed_ms,
            "count": _sign_count(result.detail),
        }
        self._state.setdefault("logs", []).insert(0, entry)
        self._state["logs"] = self._state["logs"][:_LOG_LIMIT]
        self._state.setdefault("last", {})[site.id] = entry
        self._save_state()
        # 同步写一条到全局日志（面板「日志」标签里能看到几点签的、结果、失败原因）
        logger.info(
            "站点签到 %s → %s（%sms）：%s",
            entry["name"],
            STATE_LABELS.get(result.state, result.state),
            result.elapsed_ms,
            result.detail or "-",
        )
        return entry

    # ------------------------------------------------------------------ 调度
    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("站点签到轮询失败")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=max(10, int(self.config.poll_interval_seconds or 30))
                )
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        if not self.config.enabled:
            return
        now = datetime.now()
        for site in self.config.sites:
            if not site.enabled or not site.url:
                continue
            last_entry = (self._state.get("last") or {}).get(site.id) or {}
            last_run: datetime | None = None
            if last_entry.get("at"):
                try:
                    last_run = datetime.fromisoformat(str(last_entry["at"]))
                except Exception:  # noqa: BLE001
                    last_run = None
            if not is_due(site, now, last_run):
                continue
            result = await self._run(site)
            result = self._refine_already(site, result)
            self._record(site, result)
            await self._notify(site, result)
            self._sync_card()

    async def run_now(self, site_id: str) -> dict[str, Any]:
        """面板上的「立即签到」按钮。"""
        site = next((item for item in self.config.sites if item.id == site_id), None)
        if site is None:
            return {"ok": False, "error": "没有这个站点"}
        result = await self._run(site)
        result = self._refine_already(site, result)
        entry = self._record(site, result)
        self._sync_card()
        return {
            "ok": True,
            "state": result.state,
            "detail": result.detail,
            "entry": entry,
            "page": result.page_text,
            "final_url": result.final_url,
        }

    def set_flaresolverr_default(self, endpoint: str) -> None:
        """站点签到自己没填 FlareSolverr 时，沿用磁力导入那台的地址。"""
        if not self.config.flaresolverr and endpoint:
            self.config.flaresolverr = endpoint

    async def _run(self, site: SiteCheckinSiteConfig) -> SignResult:
        logger.info("站点签到开始：%s (%s)", site.name or site.id, site.url)
        result = await asyncio.to_thread(
            sign_site,
            url=site.url,
            cookie=site.cookie,
            method=site.method,
            sign_url=site.sign_url,
            post_data=site.post_data,
            headers=site.headers,
            success_keywords=site.success_keywords,
            already_keywords=site.already_keywords,
            timeout=self.config.timeout_seconds,
            flaresolverr=self.config.flaresolverr,
        )
        # 结果统一由 _record 写一条到全局日志，这里不再重复打一遍
        return result

    # ------------------------------------------------------------------ 通知
    def _notify_text(self, site: SiteCheckinSiteConfig, result: SignResult) -> str:
        status = {"signed": "签到成功", "already": "今日已签到", "failed": "签到失败"}.get(
            result.state, result.state
        )
        template = self.config.notify_text or "{name}\n{status}：{reason}"
        try:
            return template.format(
                name=site.name or site.id,
                status=status,
                reason=result.detail or "",
                time=datetime.now().strftime("%Y-%m-%d %H:%M"),
            )
        except Exception:  # noqa: BLE001
            return f"{site.name or site.id}\n{status}：{result.detail}"

    async def _notify(self, site: SiteCheckinSiteConfig, result: SignResult) -> None:
        if not self.config.notify_enabled:
            return
        text = self._notify_text(site, result)
        token = self.config.notify_bot_token
        chat = self.config.notify_chat_id
        if not token or not chat:
            logger.info("站点签到通知未配置 bot，跳过：%s", text.replace("\n", " | "))
            return
        try:
            await asyncio.to_thread(self._send_bot_api, token, chat, text)
        except Exception:  # noqa: BLE001
            logger.warning("站点签到通知发送失败", exc_info=True)

    @staticmethod
    def _send_bot_api(token: str, chat_id: str, text: str) -> None:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        with httpx.Client(timeout=20) as client:
            client.post(url, data={"chat_id": chat_id, "text": text})

    # ------------------------------------------------------------------ 面板
    def snapshot(self) -> dict[str, Any]:
        self._sync_card()
        return self.card.config or {}

    def log_entries(self, limit: int = 60) -> list[dict[str, Any]]:
        return list(self._state.get("logs", []))[: max(1, limit)]
