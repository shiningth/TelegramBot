"""MagnetBoard（sehuatang-crawler）适配层：登录并调用「数据交换中心 → 导入数据」接口。

使用 momo 的资源导入任务接口；保留空 ID，由 momo 本机分配，且禁止自动推送。
"""

from __future__ import annotations

import json
import re
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from telegram_bot.shared.net import ensure_http_url

logger = logging.getLogger(__name__)


class MagnetBoardError(RuntimeError):
    pass


def import_csv_sync(
    base_url: str,
    username: str,
    password: str,
    file_path: Path,
    file_name: str,
    *,
    update_existing: bool = False,
    skip_duplicates: bool = True,
    timeout: float = 1800.0,
) -> dict[str, Any]:
    """同步登录并上传 CSV，返回导入接口的 JSON。请在 asyncio.to_thread 中调用。"""
    import httpx

    base = ensure_http_url(base_url).rstrip("/")
    # 上传/导入耗时随文件大小增长，读超时放宽到 timeout；连接超时保持较短
    client_timeout = httpx.Timeout(timeout, connect=15.0)
    with httpx.Client(timeout=client_timeout, follow_redirects=False) as client:
        login = client.post(
            f"{base}/api/auth/login",
            json={"username": username, "password": password, "remember_me": False},
        )
        login_data = _json_or_empty(login)
        if login.status_code >= 400 or login_data.get("success") is False:
            raise MagnetBoardError(f"MagnetBoard 登录失败（{login.status_code}）：{_error_text(login_data, login)}")

        headers: dict[str, str] = {}
        token = login_data.get("access_token") or login_data.get("token")
        if isinstance(token, str) and token:
            headers["Authorization"] = f"Bearer {token}"

        with Path(file_path).open("rb") as fh:
            response = client.post(
                f"{base}/api/data/import-jobs",
                headers=headers,
                files={"file": (file_name, fh, "text/csv")},
                params={
                    "submission_key": str(uuid.uuid4()),
                    "update_existing": "true" if update_existing else "false",
                    "auto_push": "false",
                },
            )
        payload = _json_or_empty(response)
        if response.status_code >= 400 or payload.get("success") is False:
            raise MagnetBoardError(f"MagnetBoard 导入失败（{response.status_code}）：{_error_text(payload, response)}")
        job_id = payload.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise MagnetBoardError("momo 资源导入接口未返回任务 ID，无法确认导入结果")
        # 新接口以 update_existing=false 表示跳过重复记录，不提供重复新增模式。
        if not skip_duplicates and not update_existing:
            logger.info("momo resource import always skips duplicates when update_existing is false")
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MagnetBoardError(f"momo 资源导入等待超时（任务 {job_id}），请在 momo 确认任务状态")
            status = client.get(
                f"{base}/api/data/import-jobs/{quote(job_id, safe='')}",
                headers=headers,
                timeout=httpx.Timeout(min(remaining, 30.0), connect=min(remaining, 15.0)),
            )
            result = _json_or_empty(status)
            if status.status_code >= 400 or result.get("success") is False:
                raise MagnetBoardError(f"momo 读取导入任务失败（任务 {job_id}）：{_error_text(result, status)}")
            job = result.get("data") if isinstance(result.get("data"), dict) else result
            state = job.get("status")
            if state in {"failed", "cancelled", "canceled"}:
                raise MagnetBoardError(f"momo 资源导入失败（任务 {job_id}）：{job.get('error') or job.get('message') or state}")
            if state == "completed":
                stats = job.get("stats")
                if not isinstance(stats, dict):
                    raise MagnetBoardError(f"momo 资源导入缺少结果统计（任务 {job_id}）")
                if int(stats.get("error_records") or 0):
                    raise MagnetBoardError(f"momo 资源导入存在失败记录（任务 {job_id}）：{summarize_import_result(stats)}")
                return {"success": True, "job_id": job_id, "data": stats}
            if state not in {"pending", "queued", "running", "processing", "uploaded", "validating"}:
                raise MagnetBoardError(f"momo 返回未知导入状态（任务 {job_id}）：{state}")
            time.sleep(min(1.5, max(0.0, deadline - time.monotonic())))


def summarize_import_result(payload: dict[str, Any], limit: int = 300) -> str:
    """把导入接口返回压缩成一行日志/状态文本。"""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    text = json.dumps(data, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _login(base: str, username: str, password: str, client: Any) -> dict[str, str]:
    login = client.post(
        f"{base}/api/auth/login",
        json={"username": username, "password": password, "remember_me": False},
    )
    data = _json_or_empty(login)
    if login.status_code >= 400 or data.get("success") is False:
        raise MagnetBoardError(f"MagnetBoard 登录失败（{login.status_code}）：{_error_text(data, login)}")
    token = data.get("access_token") or data.get("token")
    return {"Authorization": f"Bearer {token}"} if isinstance(token, str) and token else {}


def find_existing_sync(
    base_url: str,
    username: str,
    password: str,
    *,
    code: str,
    ed2k: str,
    source_site: str = "ltx",
    timeout: float = 30.0,
) -> dict[str, Any] | None:
    """按番号查 momo，命中同源且「ed2k 文件哈希一致」时返回该记录，用于导入前排重。

    注意：只比对 32 位文件哈希，不比对整条链接——同一条片子在不同来源里的文件名不一样
    （例如 www.98T.la@ABP-975-U-LTX.mp4 与 98T@ABP-975-U_LTX.mp4），按整串比会漏判。
    """
    import httpx

    base = ensure_http_url(base_url).rstrip("/")
    wanted = magnet_fingerprint(ed2k)
    if not code or not wanted:
        return None
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=False) as client:
        headers = _login(base, username, password, client)
        response = client.get(
            f"{base}/api/magnets",
            headers=headers,
            params={"q": code, "page_size": 50},
        )
        payload = _json_or_empty(response)
        if response.status_code >= 400 or payload.get("success") is False:
            raise MagnetBoardError(f"momo 查询失败（{response.status_code}）：{_error_text(payload, response)}")
        block = payload.get("data")
        rows = block.get("data") if isinstance(block, dict) else block
        return _find_in_rows(rows, source_site=source_site, wanted=wanted)


def _find_in_rows(rows: Any, *, source_site: str, wanted: str) -> dict[str, Any] | None:
    """在一批查询结果里找「同源 + 同一 32 位文件哈希」的那条。"""
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("source_site") or "") != source_site:
            continue
        magnets = item.get("magnets") or []
        if isinstance(magnets, str):
            try:
                magnets = json.loads(magnets)
            except ValueError:
                magnets = [magnets]
        for magnet in magnets:
            if magnet_fingerprint(str(magnet)) == wanted:
                return item
    return None


class ExistingLookup:
    """一次登录、多线程复用的预检查询器。

    逐行调用 `find_existing_sync` 时每行都要重新登录 momo，几千行的 CSV 会跑成几十分钟；
    这里共享一个 `httpx.Client`（线程安全）和登录头，并发查询把它压回分钟级。
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        source_site: str = "ltx",
        timeout: float = 30.0,
    ) -> None:
        self._base = ensure_http_url(base_url).rstrip("/")
        self._username = username
        self._password = password
        self._source_site = source_site
        self._timeout = timeout
        self._client: Any = None
        self._headers: dict[str, str] = {}
        self._lock = threading.Lock()
        self._index: dict[str, set[str]] | None = None

    def __enter__(self) -> "ExistingLookup":
        self.open()
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False

    def open(self) -> "ExistingLookup":
        if self._client is not None:
            return self
        with self._lock:
            if self._client is None:
                import httpx

                client = httpx.Client(
                    timeout=httpx.Timeout(self._timeout, connect=10.0),
                    follow_redirects=False,
                )
                try:
                    self._headers = _login(self._base, self._username, self._password, client)
                except Exception:
                    client.close()
                    raise
                self._client = client
        return self

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()

    def prime(self, source_site: str = "", *, page_size: int = 100) -> int:
        """把某个来源的全部记录一次性拉下来建索引（番号 → 指纹集合）。

        momo 单条查询约 0.9 秒且并发收益有限（实测 8 线程只到 2 行/秒），几千行的表逐条查
        要几十分钟；这里按 `source_site` 分页把整表取回（接口 page_size 上限 100），本地比对。
        """
        site = source_site or self._source_site
        self.open()
        index: dict[str, set[str]] = {}
        page = 1
        total = 0
        while True:
            response = self._client.get(
                f"{self._base}/api/magnets",
                headers=self._headers,
                params={"source_site": site, "page": page, "page_size": page_size},
            )
            payload = _json_or_empty(response)
            if response.status_code >= 400 or payload.get("success") is False:
                raise MagnetBoardError(f"momo 查询失败（{response.status_code}）：{_error_text(payload, response)}")
            block = payload.get("data")
            rows = block.get("data") if isinstance(block, dict) else block
            rows = rows or []
            for item in rows:
                if not isinstance(item, dict):
                    continue
                if str(item.get("source_site") or "") != site:
                    continue
                code = str(item.get("code") or "").strip().upper()
                if not code:
                    continue
                bucket = index.setdefault(code, set())
                bucket.add(code.replace("-", ""))
                raw_hash = str(item.get("magnet_hash") or "").strip().lower()
                if raw_hash:
                    bucket.add(raw_hash)
                magnets = item.get("magnets") or []
                if isinstance(magnets, str):
                    try:
                        magnets = json.loads(magnets)
                    except ValueError:
                        magnets = [magnets]
                for magnet in magnets:
                    bucket.add(magnet_fingerprint(str(magnet)))
                total += 1
            pages = block.get("total_pages") if isinstance(block, dict) else None
            if not rows or (isinstance(pages, int) and page >= pages):
                break
            page += 1
        self._index = index
        logger.info("momo precheck index primed: site=%s rows=%d codes=%d", site, total, len(index))
        return total

    def find(self, code: str, ed2k: str, source_site: str = "") -> dict[str, Any] | None:
        wanted = magnet_fingerprint(ed2k)
        if not code or not wanted:
            return None
        if self._index is not None:
            # 索引已经整表拉过：本地比对，不再打接口
            bucket = self._index.get(code.strip().upper())
            return {"code": code} if bucket and wanted in bucket else None
        self.open()
        response = self._client.get(
            f"{self._base}/api/magnets",
            headers=self._headers,
            params={"q": code, "page_size": 50},
        )
        payload = _json_or_empty(response)
        if response.status_code >= 400 or payload.get("success") is False:
            raise MagnetBoardError(f"momo 查询失败（{response.status_code}）：{_error_text(payload, response)}")
        block = payload.get("data")
        rows = block.get("data") if isinstance(block, dict) else block
        return _find_in_rows(rows, source_site=source_site or self._source_site, wanted=wanted)

    def __call__(self, code: str, ed2k: str, source_site: str = "") -> bool:
        return self.find(code, ed2k, source_site) is not None


_ED2K_HASH = re.compile(r"\|([0-9A-Fa-f]{32})\|")
_BTIH_HASH = re.compile(r"btih:([0-9A-Za-z]{32,40})")


def magnet_fingerprint(value: str) -> str:
    """取磁链的文件指纹：ed2k 取 32 位 hash，BT 取 btih，其它退回原文小写。"""
    text = str(value or "")
    match = _ED2K_HASH.search(text)
    if match:
        return match.group(1).lower()
    match = _BTIH_HASH.search(text)
    if match:
        return match.group(1).lower()
    return text.strip().lower()


def import_rows_sync(
    base_url: str,
    username: str,
    password: str,
    rows: list[dict[str, Any]],
    *,
    update_existing: bool = False,
    skip_duplicates: bool = True,
    timeout: float = 1800.0,
) -> dict[str, Any]:
    """把若干行数据写成临时 CSV 再走导入接口（列名与 momo 表字段一致）。"""
    import csv
    import tempfile
    from pathlib import Path

    if not rows:
        return {"success": True, "data": {"imported_records": 0, "updated_records": 0, "skipped_records": 0}}
    columns = [
        "title",
        "content",
        "url",
        "images",
        # cover_url 必须一起写：momo 列表优先用它，缺了就显示不出封面
        "cover_url",
        "magnets",
        "code",
        "size",
        "is_uncensored",
        "author",
        "forum_id",
        "forum_type",
        "forum_name",
        "magnet_hash",
        "source_site",
        "published_at",
    ]
    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="")
    try:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})
        handle.close()
        return import_csv_sync(
            base_url,
            username,
            password,
            Path(handle.name),
            "ltx-watch.csv",
            update_existing=update_existing,
            skip_duplicates=skip_duplicates,
            timeout=timeout,
        )
    finally:
        try:
            Path(handle.name).unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove temp import CSV %s", handle.name, exc_info=True)


def _json_or_empty(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {"data": data}


def _error_text(payload: dict[str, Any], response: Any) -> str:
    for key in ("detail", "message", "error"):
        value = payload.get(key)
        if value:
            return str(value)
    return str(getattr(response, "text", "") or "")[:200] or "未知错误"
