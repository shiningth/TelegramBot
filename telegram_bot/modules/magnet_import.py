from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from telegram_bot.config import MagnetImportConfig, magnet_import_config_from_dict
from telegram_bot.shared.contracts import HandlerResult
from telegram_bot.shared.csv_prepare import prepare_csv
from telegram_bot.shared.magnetboard_api import ExistingLookup, import_csv_sync, summarize_import_result
from telegram_bot.shared.telegram_download import document_name, document_size, download_document_resumable

from .base import ModuleCard

logger = logging.getLogger(__name__)

Importer = Callable[..., dict[str, Any]]
_STATE_LIMIT = 200
_FIELD_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_MENTION_PATTERN = re.compile(r"^@([A-Za-z0-9_]+)\s+(\S.*)$", re.S)


def render_reply(template: str, **values: Any) -> str:
    """渲染回复模板：只替换已知占位符，未知的 {xxx} 原样保留，且永不抛出异常。"""

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(values[key]) if key in values else match.group(0)

    return _FIELD_PATTERN.sub(_replace, str(template or ""))


def _is_command(value: str, candidates: list[str]) -> bool:
    target = value.strip().casefold()
    if not target:
        return False
    return any(target == str(item).strip().casefold() for item in candidates if str(item).strip())


def human_size(size: int) -> str:
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class MagnetImportModule:
    """监听指定群/用户发送的 CSV，下载后导入 MagnetBoard 并回复。"""

    def __init__(
        self,
        card: ModuleCard,
        config: MagnetImportConfig,
        project_root: Path | None = None,
        *,
        importer: Importer = import_csv_sync,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.card = card
        self.config = config
        self.project_root = project_root or Path.cwd()
        self._importer = importer
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._in_progress: set[str] = set()
        self._state = self._load_state()
        self._sync_card_config()
        self.card.metrics = self._metrics()

    async def start(self) -> None:
        self._clean_download_dir()
        self.card.status = "running"

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self.card.status = "stopped"

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def handle_message(self, event: Any) -> HandlerResult:
        if "MessageEdited" in type(event).__qualname__:
            return HandlerResult.ignored(self.card.id)
        message = getattr(event, "message", event)
        chat_id = str(getattr(message, "chat_id", getattr(event, "chat_id", "")) or "")
        if chat_id not in set(self.config.watch_chats):
            return HandlerResult.ignored(self.card.id)
        client = getattr(event, "client", None)
        command = None
        command_label = ""
        if self.config.manual_only:
            text = str(getattr(message, "raw_text", None) or getattr(message, "message", "") or "").strip()
            matched = self._match_manual_command(text)
            if matched is None or client is None:
                return HandlerResult.ignored(self.card.id)
            kind, word, target = matched
            command_label = f"@{target} {word}" if kind == "mention" else word
            if target:
                me = await client.get_me()
                if str(getattr(me, "username", "") or "").casefold() != target.casefold():
                    return HandlerResult.ignored(self.card.id)
            allowed = await self._sender_matches(message)
            if not allowed and not self.config.sender_ids and not self.config.sender_usernames:
                try:
                    permissions = await client.get_permissions(int(chat_id), message.sender_id)
                    allowed = bool(permissions.is_admin or permissions.is_creator)
                except Exception:
                    allowed = False
            if not allowed:
                if self.config.silent_denied:
                    self._note_denied(message)
                    return HandlerResult.ignored(self.card.id)
                await self._reply(client, int(chat_id), self.config.reply_denied_text, reply_to=message.id)
                return HandlerResult.handled(self.card.id, "manual import denied")
            command = message
            message = await command.get_reply_message()
            if message is None or str(getattr(message, "chat_id", "")) != chat_id:
                await self._reply(
                    client,
                    int(chat_id),
                    self.config.reply_need_reply_text,
                    reply_to=command.id,
                    command=command_label,
                )
                return HandlerResult.handled(self.card.id, "manual import needs CSV reply")
        name = document_name(message)
        if not getattr(message, "document", None) or not name or not self._name_matches(name):
            if command is not None:
                await self._reply(
                    client,
                    int(chat_id),
                    self.config.reply_not_csv_text,
                    reply_to=command.id,
                    file=name,
                )
            return HandlerResult.ignored(self.card.id)
        if command is None and not await self._sender_matches(message):
            return HandlerResult.ignored(self.card.id)

        key = f"{chat_id}:{getattr(message, 'id', '')}"
        if key in self._in_progress or key in self._state.get("processed", {}):
            if command is not None:
                await self._reply(
                    client,
                    int(chat_id),
                    self.config.reply_duplicate_text,
                    reply_to=command.id,
                    file=name,
                )
            return HandlerResult.ignored(self.card.id)

        size = document_size(message)
        if size > self.config.max_file_size_mb * 1024 * 1024:
            logger.warning("Magnet import skipped oversized file %s (%s bytes)", name, size)
            self._record(key, name, "skipped", f"文件超过 {self.config.max_file_size_mb} MB")
            if command is not None:
                await self._reply(
                    client,
                    int(chat_id),
                    self.config.reply_oversized_text,
                    reply_to=command.id,
                    file=name,
                    size=human_size(size),
                    max_size=self.config.max_file_size_mb,
                )
            return HandlerResult.handled(self.card.id, "magnet import skipped oversized file")
        if not self.config.api_username or not self.config.api_password:
            logger.warning("Magnet import skipped %s: MagnetBoard username/password not configured", name)
            self._set_last(name, "failed", "未配置 MagnetBoard 用户名/密码")
            if command is not None:
                await self._reply(client, int(chat_id), self.config.reply_missing_credentials_text, reply_to=command.id)
            return HandlerResult.handled(self.card.id, "magnet import missing credentials")

        client = getattr(event, "client", None)
        if client is None or not hasattr(client, "iter_download"):
            return HandlerResult.failed(self.card.id, "Telegram client cannot download files")

        self._in_progress.add(key)
        task = asyncio.create_task(self._process(client, message, key, name, command=command))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info("Magnet import queued: chat_id=%s message_id=%s file=%s size=%s", chat_id, message.id, name, size)
        return HandlerResult.handled(self.card.id, "magnet import queued", {"file": name, "size": size})

    async def _process(self, client: Any, message: Any, key: str, name: str, *, command: Any = None) -> None:
        dest = self._download_dir() / f"{message.id}_{_safe_name(name)}"
        try:
            async with self._lock:
                self._set_last(name, "downloading", "")
                started = time.monotonic()
                size = await download_document_resumable(
                    client,
                    message,
                    dest,
                    retries=self.config.download_retries,
                    refetch=lambda: _refetch(client, message),
                    sleep=self._sleep,
                )
                logger.info("Magnet import downloaded %s (%s bytes) in %.1fs", name, size, time.monotonic() - started)

                self._set_last(name, "importing", "")
                with contextlib.ExitStack() as stack:
                    lookup = self._existing_lookup()
                    if lookup is not None and hasattr(lookup, "__enter__"):
                        try:
                            stack.enter_context(lookup)
                        except Exception:
                            # 登录不上就不预检，交给 momo 自己判重（导入不该因此中断）
                            logger.warning("magnet import precheck login failed, skipping precheck", exc_info=True)
                            lookup = None
                        else:
                            prime = getattr(lookup, "prime", None)
                            if prime is not None:
                                self._set_last(name, "importing", "读取 momo 索引…")
                                try:
                                    rows = await asyncio.to_thread(prime)
                                    logger.info("Magnet import %s: precheck index rows=%s", name, rows)
                                except Exception:
                                    # 拉索引失败就退回逐条查询，慢一点但结果一样
                                    logger.warning("magnet import precheck index failed, falling back to per-row queries", exc_info=True)
                    prepared = await asyncio.to_thread(
                        prepare_csv,
                        dest,
                        lookup=lookup,
                        progress=self._precheck_progress(name),
                    )
                logger.info("Magnet import precheck %s: %s", name, prepared.summary())
                upload = prepared.path
                try:
                    if prepared.rows == 0:
                        payload: dict[str, Any] = {
                            "success": True,
                            "data": {
                                "imported_records": 0,
                                "updated_records": 0,
                                "skipped_records": prepared.already_known,
                                "total_records": prepared.already_known,
                            },
                        }
                        logger.info("Magnet import %s: all rows already exist in momo, nothing uploaded", name)
                    else:
                        payload = await asyncio.to_thread(
                            self._importer,
                            self.config.api_base_url,
                            self.config.api_username,
                            self.config.api_password,
                            upload,
                            name,
                            update_existing=self.config.update_existing,
                            skip_duplicates=self.config.skip_duplicates,
                            timeout=self.config.import_timeout_seconds,
                        )
                finally:
                    if upload != dest:
                        try:
                            upload.unlink(missing_ok=True)
                        except OSError:
                            logger.warning("Failed to remove prepared CSV %s", upload, exc_info=True)
                summary = summarize_import_result(payload)
                logger.info("Magnet import finished %s: %s", name, summary)
                self._record(key, name, "imported", summary)

            stats = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            template = self.config.manual_success_text if command is not None else self.config.reply_text
            if template.strip():
                await self._reply(
                    client,
                    message.chat_id,
                    template,
                    reply_to=command.id if command else message.id,
                    **self._stat_values(
                        stats,
                        name,
                        {
                            "total": prepared.total,
                            "valid": prepared.rows,
                            "dupes": prepared.duplicated,
                            "invalid": prepared.invalid,
                            "existing": prepared.existing,
                            "normalized": prepared.normalized,
                        },
                    ),
                )
        except asyncio.CancelledError:
            self._set_last(name, "cancelled", "服务停止，任务已取消")
            raise
        except Exception as exc:
            logger.exception("Magnet import failed for %s", name)
            self._set_last(name, "failed", str(exc)[:300])
            if command is not None:
                await self._reply(
                    client,
                    message.chat_id,
                    self.config.reply_failed_text,
                    reply_to=command.id,
                    file=name,
                )
        finally:
            self._in_progress.discard(key)
            for path in (dest, dest.with_name(dest.name + ".part")):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Failed to remove magnet import temp file %s", path, exc_info=True)

    def update_config(self, values: dict[str, Any]) -> None:
        data = values.get("magnet_import", values)
        if not isinstance(data, dict):
            return
        data = dict(data)
        # 后台不回显密码，留空表示保持原值
        if not str(data.get("api_password") or ""):
            data.pop("api_password", None)
        self.config = magnet_import_config_from_dict({**asdict(self.config), **data})
        self._sync_card_config()

    def _sync_card_config(self) -> None:
        config = asdict(self.config)
        config["api_password"] = ""
        config["has_api_password"] = bool(self.config.api_password)
        self.card.config = config

    def _match_manual_command(self, text: str) -> tuple[str, str, str] | None:
        """匹配手动触发指令，返回 (类型, 触发词, 目标用户名)。

        用法：``/导入``、``/import``、``/import@bot``、``@bot 导入``、``@bot /import``。
        触发词与文案均来自面板配置，大小写不敏感，且不接受多余内容。
        """
        value = (text or "").strip()
        if not value:
            return None
        mention = _MENTION_PATTERN.match(value)
        if mention:
            word = mention.group(2).strip()
            candidates = [*self.config.manual_mention_commands, *self.config.manual_commands]
            return ("mention", word, mention.group(1)) if _is_command(word, candidates) else None
        head, _, rest = value.partition(" ")
        if rest.strip():
            return None
        word, target = head, ""
        if "@" in word:
            word, _, target = word.partition("@")
            if not target.strip():
                return None
        if _is_command(word, self.config.manual_commands):
            return ("command", word, target.strip())
        return None

    def _existing_lookup(self) -> Any:
        """导入前预检用的查询器；未配置账号时返回 None（只做哈希规范化）。"""
        if not (self.config.api_username and self.config.api_password):
            return None
        return ExistingLookup(
            self.config.api_base_url,
            self.config.api_username,
            self.config.api_password,
            source_site="ltx",
        )

    def _precheck_progress(self, name: str) -> Any:
        """给 prepare_csv 的进度回调：把「预检 314/2550」写到面板状态里（限流 3 秒）。"""
        state = {"at": 0.0}

        def report(done: int, total: int) -> None:
            now = time.monotonic()
            if done < total and now - state["at"] < 3.0:
                return
            state["at"] = now
            self._set_last(name, "importing", f"预检 {done}/{total}")

        return report

    def _stat_values(self, stats: dict[str, Any], name: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        def _count(key: str) -> Any:
            value = stats.get(key)
            try:
                return int(value)
            except (TypeError, ValueError):
                return value if value is not None else 0

        values = {
            "file": name,
            "imported": _count("imported_records"),
            "updated": _count("updated_records"),
            "skipped": _count("skipped_records"),
            "errors": _count("error_records"),
            "uploaded": _count("total_records"),
            "total": 0,
            "valid": 0,
            "dupes": 0,
            "invalid": 0,
            "existing": 0,
            "normalized": 0,
        }
        if extra:
            values.update({key: value for key, value in extra.items() if key in values})
        return values

    async def _reply(self, client: Any, chat_id: Any, template: str, *, reply_to: Any, **values: Any) -> None:
        """按模板回一条消息；模板为空则跳过，占位符缺失时原样保留。"""
        text = render_reply(template, **values).strip()
        if not text:
            return
        try:
            await client.send_message(int(chat_id), text, reply_to=reply_to)
        except Exception:
            logger.warning("Could not send magnet import reply", exc_info=True)

    def _name_matches(self, name: str) -> bool:
        try:
            return re.search(self.config.file_name_pattern, name, re.IGNORECASE) is not None
        except re.error:
            logger.warning("Invalid magnet import file_name_pattern: %s", self.config.file_name_pattern)
            return False

    async def _sender_matches(self, message: Any) -> bool:
        ids = {str(item).strip() for item in self.config.sender_ids if str(item).strip()}
        names = {item.lstrip("@").lower() for item in self.config.sender_usernames if item.strip()}
        if str(getattr(message, "sender_id", "")) in ids:
            return True
        if not names:
            return False
        sender = getattr(message, "sender", None)
        if sender is None and hasattr(message, "get_sender"):
            try:
                sender = await message.get_sender()
            except Exception:
                sender = None
        username = str(getattr(sender, "username", "") or "").lower()
        return bool(username) and username in names

    def _resolve(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.project_root / path

    def _download_dir(self) -> Path:
        return self._resolve(self.config.download_dir)

    def _clean_download_dir(self) -> None:
        directory = self._download_dir()
        if not directory.is_dir():
            return
        for path in directory.iterdir():
            try:
                shutil.rmtree(path) if path.is_dir() else path.unlink()
            except OSError:
                logger.warning("Failed to clean magnet import leftover %s", path, exc_info=True)

    def _load_state(self) -> dict[str, Any]:
        path = self._resolve(self.config.state_store)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"processed": {}, "imported": 0}
        if not isinstance(data, dict) or not isinstance(data.get("processed"), dict):
            return {"processed": {}, "imported": 0}
        return data

    def _save_state(self) -> None:
        path = self._resolve(self.config.state_store)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            logger.warning("Failed to save magnet import state", exc_info=True)

    def _metrics(self) -> dict[str, Any]:
        return {
            "imported": int(self._state.get("imported", 0)),
            "denied": int(self._state.get("denied", 0)),
            "last": self._state.get("last") or {},
        }

    def _note_denied(self, message: Any) -> None:
        """静默拦截非授权用户的指令：群里不发言，只在后台计数并留一行日志。"""
        self._state["denied"] = int(self._state.get("denied", 0)) + 1
        self.card.metrics = self._metrics()
        logger.info(
            "Magnet import ignored unauthorized command: chat_id=%s sender_id=%s message_id=%s text=%s",
            getattr(message, "chat_id", ""),
            getattr(message, "sender_id", ""),
            getattr(message, "id", ""),
            str(getattr(message, "raw_text", "") or "")[:40],
        )
        self._save_state()

    def _set_last(self, name: str, status: str, detail: str) -> None:
        last = {"file": name, "status": status, "detail": detail, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        self._state["last"] = last
        self.card.metrics = self._metrics()
        if status not in {"downloading", "importing"}:
            self._save_state()

    def _record(self, key: str, name: str, status: str, detail: str) -> None:
        processed = self._state.setdefault("processed", {})
        processed[key] = {"file": name, "status": status, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        while len(processed) > _STATE_LIMIT:
            processed.pop(next(iter(processed)))
        if status == "imported":
            self._state["imported"] = int(self._state.get("imported", 0)) + 1
        self._set_last(name, status, detail)


async def _refetch(client: Any, message: Any) -> Any:
    if not hasattr(client, "get_messages"):
        return message
    return await client.get_messages(message.chat_id, ids=message.id)


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", name).strip("._")
    return cleaned[:120] or "data.csv"
