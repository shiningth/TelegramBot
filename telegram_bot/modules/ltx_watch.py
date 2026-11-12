"""LTX 频道自动采集：用独立用户会话读频道，解析 ed2k 后写入 momo。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram_bot.config import LtxWatchConfig, MagnetImportConfig, TelegramConfig
from telegram_bot.shared.contracts import HandlerResult
from telegram_bot.shared.javdb import code_variants, fetch_meta, search_detail_by_code
from telegram_bot.shared.magnetboard_api import find_existing_sync, import_rows_sync, summarize_import_result

from .base import ModuleCard
from .checkin import cron_matches
from .magnet_import import render_reply

logger = logging.getLogger(__name__)

_STATE_LIMIT = 5000
_SAMPLE_LIMIT = 5

# 频道里部分消息在 hash 后还有 115 秒传参数 h=<base32>，必须整段吃进来，
# 否则链接会被截成 "...|HASH|"（缺收尾斜杠）并被判 unsupported，
# 而残留的 h=... 会漏进标题。
_ED2K = re.compile(r"ed2k://\|file\|[^|\r\n]+\|\d+\|[0-9A-Fa-f]{32}\|(?:[^|\r\n]+\|)?/?")
# 番号必须以字母开头：259LUXU-1900 只取 LUXU-1900，300MAAN-691 只取 MAAN-691
_CODE = re.compile(r"([A-Za-z]{2,6}-\d{2,5})")
# FC2：FC2-1234567 / FC2-PPV-1234567 统一成 FC2-1234567
_FC2 = re.compile(r"FC2[-_ ]?(?:PPV[-_ ]?)?(\d{5,8})", re.IGNORECASE)
_TITLE_JUNK = re.compile(r"#(?:LTX|ltx|98T|自制|[0-9]{0,3}[A-Za-z]{2,6}-\d{2,5})")
_LEADING_NOISE = re.compile(r"^[^\w\u3040-\u30ff\u4e00-\u9fff]+")
_NUMERIC_ID = re.compile(r"^-?\d+$")
_TME_LINK = re.compile(r"(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]+)")


def normalize_channel(value: Any) -> Any:
    """把面板里填的频道整理成 get_entity 认识的形式。

    支持用户名（带不带 @ 都行）、t.me 链接、数字 ID（-100…）。
    频道主取消用户名后，只能用数字 ID，因此这里必须能识别纯数字。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    match = _TME_LINK.search(text)
    if match:
        text = match.group(1)
    if text.startswith("@"):
        text = text[1:]
    if _NUMERIC_ID.match(text):
        return int(text)
    return text


def human_size(size: int) -> str:
    value = float(max(0, int(size)))
    if value >= 1024 ** 3:
        return f"{value / 1024 ** 3:.2f} GB"
    if value >= 1024 ** 2:
        return f"{value / 1024 ** 2:.0f} MB"
    return f"{value / 1024:.0f} KB"


def _daily_cron_time(expression: str) -> tuple[int, int] | None:
    """解析「0 21 * * *」这类每天固定时刻，返回 (时, 分)；其它写法返回 None。"""
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


@dataclass
class ParsedItem:
    message_id: int
    code: str
    title: str
    ed2k: str
    size_bytes: int
    published_at: datetime | None = None
    javdb_url: str = ""
    author: str = ""
    cover_url: str = ""
    msg_time: str = ""
    is_fc2: bool = False

    @property
    def size_text(self) -> str:
        return human_size(self.size_bytes)

    @property
    def magnet_hash(self) -> str:
        parts = self.ed2k.split("|")
        # ed2k://|file|名字|大小|hash|/
        return parts[4].lower() if len(parts) >= 5 else ""


def parse_message(text: str, *, message_id: int = 0, date: datetime | None = None, buttons: list[str] | None = None) -> ParsedItem | None:
    """从频道消息正文里解析标题、番号、ed2k、大小与 JavDB 链接。"""
    body = (text or "").replace("\u200b", "").strip()
    match = _ED2K.search(body)
    if not match:
        return None
    ed2k = match.group(0)
    parts = ed2k.split("|")
    if len(parts) < 5:
        return None
    filename = parts[2]
    try:
        size_bytes = int(parts[3])
    except (IndexError, ValueError):
        size_bytes = 0

    code = ""
    is_fc2 = False
    fc2 = _FC2.search(body) or _FC2.search(filename)
    if fc2:
        code = f"FC2-{fc2.group(1)}"
        is_fc2 = True
    else:
        tagged = _CODE.search(body)
        if tagged:
            code = tagged.group(1).upper()
        else:
            from_file = _CODE.search(filename)
            if from_file:
                code = from_file.group(1).upper()
            else:
                anywhere = _CODE.search(body)
                code = anywhere.group(1).upper() if anywhere else ""

    title = body.replace(ed2k, " ")
    title = _TITLE_JUNK.sub(" ", title)
    title = _LEADING_NOISE.sub("", title)
    title = " ".join(title.split()).strip(" -|·")
    if not title:
        title = code or filename

    javdb = ""
    for url in buttons or []:
        if url and "javdb.com" in url:
            javdb = url
            break

    msg_time = ""
    if date is not None:
        local = date.astimezone() if date.tzinfo is not None else date
        msg_time = local.strftime("%Y-%m-%d %H:%M")
        date = datetime(local.year, local.month, local.day, local.hour, local.minute, local.second)
    return ParsedItem(
        message_id=message_id,
        code=code,
        title=title,
        ed2k=ed2k,
        size_bytes=size_bytes,
        published_at=date,
        javdb_url=javdb,
        msg_time=msg_time,
        is_fc2=is_fc2,
    )


class LtxWatchModule:
    """轮询 LTX 频道，把新消息里的 ed2k 转成 momo 资源记录。"""

    def __init__(
        self,
        card: ModuleCard,
        config: LtxWatchConfig,
        magnet: MagnetImportConfig,
        telegram: TelegramConfig,
        project_root: Path | None = None,
        *,
        importer: Callable[..., dict[str, Any]] = import_rows_sync,
        lookup: Callable[..., dict[str, Any] | None] = find_existing_sync,
        meta_fetcher: Callable[..., dict[str, str] | None] = fetch_meta,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.card = card
        self.config = config
        self.magnet = magnet
        self.telegram = telegram
        self.project_root = project_root or Path.cwd()
        self._importer = importer
        self._lookup = lookup
        self._meta = meta_fetcher
        # 按番号去 JavDB 搜索；单独挂一个属性，方便测试替换、不用真连网
        self._search = search_detail_by_code
        self._sleep = sleep
        self._client: Any = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._dryrun_meta_ids: set[int] = set()
        self._state = self._load_state()
        self._sync_card_config()
        self._refresh_metrics()

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        self.card.status = "running"
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="ltx-watch")
        logger.info("LTX watch started: channel=%s interval=%ss dry_run=%s", self.config.channel, self.config.poll_interval_seconds, self.config.dry_run)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self._disconnect()
        self.card.status = "stopped"

    async def handle_message(self, event: Any) -> HandlerResult:
        return HandlerResult.ignored(self.card.id)

    def update_config(self, values: dict[str, Any]) -> None:
        data = values.get("ltx_watch", values)
        if not isinstance(data, dict):
            return
        from telegram_bot.config import ltx_watch_config_from_dict

        was_dry_run = self.config.dry_run
        self.config = ltx_watch_config_from_dict({**self._asdict(), **data})
        if was_dry_run and not self.config.dry_run:
            # 从试运行切到真导入：清掉试运行留下的“已处理”记录，让历史消息重新走一遍
            processed = self._state.get("processed", {})
            for key in [k for k, v in processed.items() if v.get("status") == "dry-run"]:
                processed.pop(key, None)
            self._state["last_message_id"] = 0
            self._state["window"] = {"since": self._now_text(), "imported": 0, "skipped": 0, "failed": 0}
            self._save_state()
            logger.info("LTX watch switched to live mode: 已清空试运行记录，下一轮重新扫描")
        self._sync_card_config()
        self._refresh_metrics()

    def _asdict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self.config)

    async def run_once(self) -> dict[str, Any]:
        """手动触发一次同步（面板按钮用）。"""
        return await self._tick()

    async def list_dialogs(self) -> list[dict[str, Any]]:
        """用采集的用户会话列出群与频道——bot 账号无权调用 GetDialogs。"""
        client = await self._ensure_client()
        dialogs = await client.get_dialogs()
        groups: list[dict[str, Any]] = []
        for dialog in dialogs:
            entity = getattr(dialog, "entity", None)
            is_group = bool(getattr(dialog, "is_group", False))
            megagroup = bool(getattr(entity, "megagroup", False))
            broadcast = bool(getattr(entity, "broadcast", False))
            is_channel = bool(getattr(dialog, "is_channel", False))
            if not (is_group or is_channel or megagroup or broadcast):
                continue
            chat_id = str(getattr(entity, "id", getattr(dialog, "id", "")))
            if chat_id and not chat_id.startswith("-100") and (is_channel or megagroup or broadcast):
                chat_id = f"-100{chat_id}"
            groups.append(
                {
                    "id": chat_id,
                    "title": getattr(dialog, "title", "") or chat_id,
                    "username": getattr(entity, "username", "") or "",
                    "kind": "channel" if broadcast else "group",
                    "megagroup": megagroup,
                    "broadcast": broadcast,
                    "selected": False,
                }
            )
        groups.sort(key=lambda item: item["title"].lower())
        return groups

    # ---------------------------------------------------------------- 内部循环
    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
                await self._maybe_notify()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("LTX watch tick failed")
                # 面板上直接显示可操作原因（例如「用户名已失效，请改填数字 ID」）
                detail = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
                self._record_error(detail[:120] or "同步失败，详见日志")
            await self._sleep(max(30, int(self.config.poll_interval_seconds)))

    async def _resolve_channel(self, client: Any) -> Any:
        """解析监控频道；用户名被取消时给出可操作的提示而不是堆栈。"""
        target = normalize_channel(self.config.channel)
        try:
            return await client.get_entity(target)
        except Exception as exc:  # noqa: BLE001 - 分类型看下面
            from telethon.errors import UsernameInvalidError, UsernameNotOccupiedError

            hint = (
                f"频道 {self.config.channel} 的用户名已失效（可能被频道主取消），"
                "请把「监控频道」改成频道数字 ID（形如 -1001234567890）后保存"
            )
            if isinstance(exc, (UsernameNotOccupiedError, UsernameInvalidError)):
                raise RuntimeError(hint) from exc
            if isinstance(exc, ValueError) and "as username" in str(exc):
                raise RuntimeError(hint) from exc
            raise

    async def _tick(self) -> dict[str, Any]:
        async with self._lock:
            started = time.monotonic()
            result = {"scanned": 0, "imported": 0, "skipped": 0, "failed": 0, "parsed": 0}
            if not str(self.config.channel or "").strip():
                logger.warning("LTX watch skipped: 未配置监控频道")
                result["error"] = "未配置监控频道"
                return result
            client = await self._ensure_client()
            entity = await self._resolve_channel(client)
            last_id = int(self._state.get("last_message_id") or 0)
            limit = 100
            if not last_id and self.config.initial_full_scan:
                limit = int(self.config.max_initial_messages)
            messages = await client.get_messages(entity, min_id=last_id, limit=limit)
            items = sorted(messages, key=lambda m: m.id)
            self._dryrun_meta_ids = {m.id for m in items[-5:]} if self.config.dry_run else set()
            result["total_seen"] = len(items)
            stop_after = int(self.config.backfill_stop_after or 0)
            consecutive_existing = 0
            for index, message in enumerate(items, start=1):
                key = str(message.id)
                if key in self._state.get("processed", {}):
                    continue
                result["scanned"] += 1
                self.card.metrics["progress"] = {
                    "done": index,
                    "total": len(items),
                    "imported": result.get("imported", 0),
                    "skipped": result.get("skipped", 0),
                }
                parsed = parse_message(
                    getattr(message, "message", "") or "",
                    message_id=message.id,
                    date=getattr(message, "date", None),
                    buttons=self._buttons(message),
                )
                if parsed is None or not parsed.code:
                    self._mark(key, message.id, status="skipped", reason="无法解析番号")
                    result["skipped"] += 1
                    continue
                result["parsed"] += 1
                outcome = await self._handle_item(parsed)
                if self.config.dry_run and outcome == "imported":
                    result["would_import"] = result.get("would_import", 0) + 1
                else:
                    result[outcome] = result.get(outcome, 0) + 1
                # 回补时：连续 N 条都已存在就认为更早的也已入库，提前收工
                if outcome == "skipped" and "已存在" in str(self._state["processed"].get(key, {}).get("reason", "")):
                    consecutive_existing += 1
                else:
                    consecutive_existing = 0
                self._state["last_message_id"] = max(int(self._state.get("last_message_id") or 0), message.id)
                if stop_after and consecutive_existing >= stop_after:
                    result["stopped_early"] = True
                    logger.info("LTX watch 连续 %s 条已存在，提前结束本轮扫描", consecutive_existing)
                    break
            if items:
                self._state["last_message_id"] = max(int(self._state.get("last_message_id") or 0), max(m.id for m in items))
            self._state["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._state["last_duration"] = round(time.monotonic() - started, 1)
            self._save_state()
            self._refresh_metrics(result)
            logger.info("LTX watch tick done: %s", result)
            return result

    def _code_variants(self, code: str) -> list[str]:
        """查重用的番号变体：带前缀写法与去前缀写法都试。"""
        return code_variants(code) or [str(code or "")]

    async def _handle_item(self, item: ParsedItem) -> str:
        key = str(item.message_id)
        if item.is_fc2 and not self.config.fc2_import:
            # 未开启 FC2 入库：只展示，不补封面/演员，也不写入 momo
            self._mark(key, item.message_id, status="fc2", reason="FC2 仅展示，不入库", code=item.code, item=item)
            return "fc2"
        if not self.config.dry_run:
            # 老数据可能是去前缀写法（LUXU-1006 vs 259LUXU-1006），逐个变体查一遍
            existing = None
            for variant in self._code_variants(item.code):
                existing = await asyncio.to_thread(
                    self._lookup,
                    self.magnet.api_base_url,
                    self.magnet.api_username,
                    self.magnet.api_password,
                    code=variant,
                    ed2k=item.ed2k,
                    source_site=self.config.source_site,
                )
                if existing:
                    break
            if existing:
                self._mark(key, item.message_id, status="skipped", reason="momo 已存在同一 ed2k", code=item.code)
                return "skipped"

        # 试运行时只抓最新几条做样本，避免首轮全量扫描被 JavDB 拖到几十分钟
        # FC2 一律不补封面/演员
        budget_ok = ((not self.config.dry_run) or item.message_id in self._dryrun_meta_ids) and not item.is_fc2
        if self.config.fetch_metadata and budget_ok:
            delay = max(0, int(self.config.metadata_delay_seconds))
            meta: dict[str, str] | None = None
            if delay:
                await self._sleep(delay)
            # fetch_meta 内部按配置顺序：DMM → AVbase → AVWikiDB → Jav321 → MGStage → JavDB
            # 每个源只补还缺的字段，封面与演员都拿到就停
            meta = await asyncio.to_thread(
                self._meta,
                item.javdb_url,
                code=item.code,
                flaresolverr=self.config.metadata_flaresolverr,
                retries=self.config.metadata_retries,
                sources=self.config.metadata_sources,
            )
            if meta:
                item.author = meta.get("author", "") or item.author
                item.cover_url = meta.get("cover_url", "") or item.cover_url
            if not item.cover_url and not item.is_fc2:
                # 缺封面：先照常入库，记一笔日志；之后可用 scripts/repair_missing_covers.py 补
                self._note_missing_cover(item.code)

        if self.config.dry_run:
            self._mark(key, item.message_id, status="dry-run", code=item.code, item=item)
            return "imported"

        row = self._build_row(item)
        try:
            payload = await asyncio.to_thread(
                self._importer,
                self.magnet.api_base_url,
                self.magnet.api_username,
                self.magnet.api_password,
                [row],
                update_existing=False,
                skip_duplicates=True,
                timeout=self.magnet.import_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("LTX watch import failed for %s", item.code)
            self._mark(key, item.message_id, status="failed", reason=str(exc)[:200], code=item.code)
            return "failed"
        stats = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if int(stats.get("imported_records") or 0) == 0 and int(stats.get("skipped_records") or 0) > 0:
            self._mark(key, item.message_id, status="skipped", reason="momo 跳过重复", code=item.code)
            return "skipped"
        self._mark(key, item.message_id, status="imported", code=item.code, item=item, detail=summarize_import_result(payload, 160))
        return "imported"

    def _build_row(self, item: ParsedItem) -> dict[str, Any]:
        forum = self.config.forum_name
        published = item.published_at.strftime("%Y-%m-%d %H:%M:%S") if item.published_at else ""
        return {
            "title": item.title,
            "content": "",
            "url": "",
            "images": json.dumps([item.cover_url], ensure_ascii=False) if item.cover_url else "[]",
            "cover_url": item.cover_url,
            "magnets": json.dumps([item.ed2k], ensure_ascii=False),
            "code": item.code,
            "size": item.size_text,
            "is_uncensored": "true",
            "author": item.author,
            "forum_id": forum,
            "forum_type": forum,
            "forum_name": forum,
            "magnet_hash": item.magnet_hash,
            "source_site": self.config.source_site,
            "published_at": published,
        }

    # ---------------------------------------------------------------- 客户端
    async def _ensure_client(self) -> Any:
        if self._client is not None and getattr(self._client, "is_connected", lambda: False)():
            return self._client
        from telethon import TelegramClient

        session = self._session_path()
        if not Path(str(session) + ".session").is_file():
            raise RuntimeError(f"LTX 采集会话不存在：{session}.session，请先登录")
        client = TelegramClient(str(session), self.telegram.api_id, self.telegram.api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError("LTX 采集会话未登录，请重新执行登录脚本")
        self._client = client
        return client

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                logger.warning("Failed to disconnect LTX watch client", exc_info=True)

    def _session_path(self) -> Path:
        directory = Path(self.telegram.session_dir)
        if not directory.is_absolute():
            directory = self.project_root / directory
        return directory / self.config.session_name

    @staticmethod
    def _buttons(message: Any) -> list[str]:
        urls: list[str] = []
        for row in getattr(message, "buttons", None) or []:
            for button in row:
                url = getattr(button, "url", None)
                if url:
                    urls.append(url)
        return urls

    # ---------------------------------------------------------------- 通知
    @staticmethod
    def _now_text() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _window(self) -> dict[str, Any]:
        window = self._state.get("window")
        if not isinstance(window, dict):
            window = {"since": self._now_text(), "imported": 0, "skipped": 0, "failed": 0}
            self._state["window"] = window
        return window

    def _summary_text(self) -> str:
        window = self._window()
        return render_reply(
            self.config.notify_text,
            since=window.get("since", ""),
            now=self._now_text(),
            imported=window.get("imported", 0),
            skipped=window.get("skipped", 0),
            failed=window.get("failed", 0),
            pending=len(self._state.get("missing_cover", [])),
            total=int(self._state.get("imported_count", 0)),
        ).strip()

    async def _send_summary(self, *, reset: bool) -> dict[str, Any]:
        text = self._summary_text()
        if not text:
            return {"ok": False, "error": "通知文案为空"}
        if not reset:
            text = f"【测试】{text}"
        target = self.config.notify_target or "me"
        try:
            if self.config.notify_bot_token:
                # 用自己的通知 bot 发：消息由该 bot 推送，和它平时的通知一致，不会被当成命令
                await asyncio.to_thread(self._send_via_bot_api, text)
            else:
                client = await self._ensure_client()
                await client.send_message(target, text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("LTX watch notify failed")
            self._record_error("汇总通知发送失败")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
        if reset:
            stamp = self._now_text()
            self._state["last_notify_at"] = stamp
            self._state["window"] = {"since": stamp, "imported": 0, "skipped": 0, "failed": 0}
            self._save_state()
            self._refresh_metrics()
        logger.info("LTX watch notify sent via %s: %s", "bot-api" if self.config.notify_bot_token else target, text.replace("\n", " | "))
        return {"ok": True, "target": target if not self.config.notify_bot_token else (self.config.notify_chat_id or target), "text": text}

    def _send_via_bot_api(self, text: str) -> None:
        import httpx

        chat_id = self.config.notify_chat_id or self.config.notify_target
        if not chat_id:
            raise RuntimeError("缺少接收用的 chat_id")
        url = f"https://api.telegram.org/bot{self.config.notify_bot_token}/sendMessage"
        with httpx.Client(timeout=20.0) as client:
            response = client.post(url, json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400 or data.get("ok") is False:
            raise RuntimeError(f"Bot API 返回 {response.status_code}: {str(data.get('description') or '')[:120]}")

    async def notify_test(self) -> dict[str, Any]:
        """面板上的「测试通知」：立即发一条，不动统计窗口。"""
        return await self._send_summary(reset=False)

    async def _maybe_notify(self) -> None:
        if not self.config.notify_enabled or self.config.dry_run:
            # 试运行没有真实入库，发了也全是 0，直接跳过
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        # 今天已经发过就不再发
        if str(self._state.get("last_notify_at") or "")[:10] == today:
            return

        scheduled = _daily_cron_time(self.config.notify_cron)
        if scheduled is not None:
            # 每天固定时刻：到点之后的第一轮就发，晚一点也不漏（轮询是 5 分钟一轮）
            hour, minute = scheduled
            if (now.hour, now.minute) < (hour, minute):
                return
            await self._send_summary(reset=True)
            return

        # 复杂表达式退回逐分钟匹配
        try:
            if not cron_matches(self.config.notify_cron, now):
                return
        except Exception:  # noqa: BLE001
            logger.warning("Invalid notify cron: %s", self.config.notify_cron)
            return
        await self._send_summary(reset=True)

    # ---------------------------------------------------------------- 状态与指标
    def _mark(self, key: str, message_id: int, *, status: str, code: str = "", reason: str = "", item: ParsedItem | None = None, detail: str = "") -> None:
        processed = self._state.setdefault("processed", {})
        entry: dict[str, Any] = {
            "message_id": message_id,
            "code": code,
            "status": status,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if reason:
            entry["reason"] = reason
        if detail:
            entry["detail"] = detail
        if item is not None:
            entry.update(
                {
                    "title": item.title[:120],
                    "size": item.size_text,
                    "author": item.author,
                    "cover": bool(item.cover_url),
                    "msg_time": item.msg_time,
                }
            )
        processed[key] = entry
        while len(processed) > _STATE_LIMIT:
            processed.pop(next(iter(processed)))
        if status in {"imported", "skipped", "failed"} and not self.config.dry_run:
            counter = f"{status}_count"
            self._state[counter] = int(self._state.get(counter, 0)) + 1
            window = self._window()
            window[status] = int(window.get(status, 0)) + 1

    def _note_missing_cover(self, code: str) -> None:
        """缺封面时记一笔（只在面板计数 + 日志），修补交给 scripts/repair_missing_covers.py。"""
        if not code:
            return
        missing = self._state.setdefault("missing_cover", [])
        if code not in missing:
            missing.append(code)
            del missing[:-50]
        logger.warning("LTX watch 缺封面（已入库，可稍后补）：%s", code)

    def _record_error(self, message: str) -> None:
        self._state["last_error"] = {"message": message[:200], "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        self._refresh_metrics()

    def _refresh_metrics(self, last: dict[str, Any] | None = None) -> None:
        samples = [
            {
                "code": entry.get("code"),
                "title": entry.get("title", ""),
                "size": entry.get("size", ""),
                "author": entry.get("author", ""),
                "status": entry.get("status"),
                "reason": entry.get("reason", ""),
                "cover": entry.get("cover", False),
                "msg_time": entry.get("msg_time", ""),
            }
            for entry in list(self._state.get("processed", {}).values())[-_SAMPLE_LIMIT:][::-1]
        ]
        self.card.metrics = {
            "dry_run": self.config.dry_run,
            "imported": int(self._state.get("imported_count", 0)),
            "skipped": int(self._state.get("skipped_count", 0)),
            "failed": int(self._state.get("failed_count", 0)),
            "pending": len(self._state.get("missing_cover", [])),
            "last_message_id": int(self._state.get("last_message_id", 0)),
            "last_run_at": self._state.get("last_run_at", ""),
            "last_duration": self._state.get("last_duration", 0),
            "last_notify_at": self._state.get("last_notify_at", ""),
            "window": self._state.get("window", {}),
            "last_error": self._state.get("last_error", {}),
            "last_tick": last or {},
            "samples": samples,
        }

    def _sync_card_config(self) -> None:
        config = self._asdict()
        config["has_session"] = Path(str(self._session_path()) + ".session").is_file()
        config["has_api_password"] = bool(self.magnet.api_password)
        config["has_notify_bot_token"] = bool(self.config.notify_bot_token)
        config["notify_bot_token"] = ""
        config["api_base_url"] = self.magnet.api_base_url or ""
        config["api_username"] = self.magnet.api_username or ""
        self.card.config = config

    def _load_state(self) -> dict[str, Any]:
        path = self._resolve(self.config.state_store)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"processed": {}, "missing_cover": []}
        if not isinstance(data, dict):
            return {"processed": {}, "missing_cover": []}
        data.setdefault("processed", {})
        data.setdefault("missing_cover", [])
        return data

    def _save_state(self) -> None:
        path = self._resolve(self.config.state_store)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            logger.warning("Failed to save LTX watch state", exc_info=True)

    def _resolve(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.project_root / path
