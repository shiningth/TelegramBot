"""上传前的 CSV 整理：磁链指纹规范化 + 按番号预检库里已有记录。

momo 只按 `magnet_hash` 字符串判重，而库里 1908 行是数字占位哈希（sehuatang thread id），
外部 CSV 里也常见占位/空哈希 —— 只靠 momo 静默跳过，会出现「同一部片子重复入库」。
上传前先做两件事：

1. 每行按 `magnets` 里的磁链重算 32 位文件哈希写回 `magnet_hash`（ed2k 的 hash / BT 的 btih）；
2. 用「番号 + 哈希」向 momo 预检，把库里已经存在的行整行摘掉，并数出来告诉调用方。

预检失败（登录不上、接口报错）不阻断导入：记一条日志，剩下的交给 momo 自己判重。
"""

from __future__ import annotations

import csv
import json
import logging
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from telegram_bot.shared.magnetboard_api import magnet_fingerprint

logger = logging.getLogger(__name__)

# lookup(code, ed2k, source_site) -> 命中返回记录（任意真值），未命中返回 None
Lookup = Callable[..., Any]

_LINK = re.compile(r"(?:ed2k://|magnet:\?)[^\s\"',]+", re.I)
_TOKEN = re.compile(r"\S+")


@dataclass
class PrepareResult:
    """整理结果：`path` 是实际要上传的文件（无改动时就是原文件）。"""

    path: Path
    rows: int = 0
    total: int = 0
    normalized: int = 0
    existing: int = 0
    duplicated: int = 0
    invalid: int = 0
    checked: bool = False
    skipped_reason: str = ""
    existing_codes: list[str] = field(default_factory=list)

    @property
    def already_known(self) -> int:
        return self.existing + self.duplicated

    def summary(self) -> str:
        parts = [f"rows={self.rows}/{self.total}", f"normalized={self.normalized}"]
        if self.checked:
            parts.append(f"existing={self.existing}")
            parts.append(f"in_file_dupes={self.duplicated}")
        elif self.skipped_reason:
            parts.append(f"precheck={self.skipped_reason}")
        return " ".join(parts)


def first_link(value: Any) -> str:
    """从 `magnets` 单元格里取第一条磁链：支持 JSON 数组、纯文本、换行分隔。"""
    if isinstance(value, (list, tuple)):
        for item in value:
            link = first_link(item)
            if link:
                return link
        return ""
    text = str(value or "").strip()
    if not text:
        return ""
    stripped = text.lstrip()
    if stripped[:1] in "[{":
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if data is not None:
            items = data if isinstance(data, list) else data.get("magnets") or [data]
            for item in items if isinstance(items, list) else [items]:
                if isinstance(item, str) and item.strip():
                    return item.strip()
                if isinstance(item, dict):
                    for key in ("url", "link", "magnet", "hash"):
                        if item.get(key):
                            return str(item[key]).strip()
            return ""
    match = _LINK.search(text)
    if match:
        return match.group(0)
    token = _TOKEN.search(text)
    return token.group(0) if token else ""


def _open_text(path: Path) -> Any:
    """按 UTF-8（含 BOM）读取；失败再退回 GB18030（国内导出常见）。"""
    try:
        return path.open("r", encoding="utf-8-sig", newline="")
    except UnicodeDecodeError:
        return path.open("r", encoding="gb18030", newline="")


def prepare_csv(
    path: Path,
    *,
    lookup: Lookup | None = None,
    source_site: str = "",
    tmp_dir: Path | None = None,
    workers: int = 8,
    progress: Callable[[int, int], None] | None = None,
) -> PrepareResult:
    """整理 `path` 指向的 CSV，返回要上传的文件与统计；不会修改原文件。

    两遍扫描：第一遍只算「番号 + 指纹」并登记要预检的键，第二遍带着预检结果写新文件。
    这样几千行也能并发查询（`workers`），不必边读边等接口。
    """
    path = Path(path)
    result = PrepareResult(path=path)
    try:
        handle = _open_text(path)
    except OSError as exc:
        result.skipped_reason = f"读不到文件：{exc}"
        return result

    with handle:
        reader = csv.DictReader(handle)
        fields = [str(name) for name in (reader.fieldnames or [])]
        if not fields:
            result.skipped_reason = "空文件"
            return result
        if "magnets" not in fields:
            result.total = sum(1 for _ in reader)
            result.rows = result.total
            result.skipped_reason = "没有 magnets 列"
            return result

        # 第一遍：登记文件内的键与需要预检的键（保持出现顺序）
        keys: list[tuple[str, str]] = []
        seen_keys: set[tuple[str, str]] = set()
        to_check: dict[tuple[str, str], tuple[str, str]] = {}
        for row in reader:
            result.total += 1
            link = first_link(row.get("magnets"))
            fingerprint = magnet_fingerprint(link) if link else ""
            key = (str(row.get("code") or "").strip().upper(), fingerprint)
            keys.append(key)
            if not (fingerprint and key[0]):
                continue
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if lookup is not None and key not in to_check:
                site = str(row.get("source_site") or "").strip() or source_site
                to_check[key] = (link, site)

        # 第二遍的写入顺序与第一遍一致，这里再走一遍文件
        checks = _run_lookups(to_check, result, lookup=lookup, workers=workers, progress=progress)

        handle_out, tmp_path = tempfile.mkstemp(
            prefix="prepared-", suffix=".csv", dir=str(tmp_dir or path.parent)
        )

        with _open_text(path) as handle_again, open(handle_out, "w", encoding="utf-8", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            seen: set[tuple[str, str]] = set()
            for row in csv.DictReader(handle_again):
                link = first_link(row.get("magnets"))
                fingerprint = magnet_fingerprint(link) if link else ""
                if fingerprint and fingerprint != str(row.get("magnet_hash") or "").strip():
                    row["magnet_hash"] = fingerprint
                    result.normalized += 1
                keep = True
                if not fingerprint:
                    # 没有可解析的磁链：照旧上传，但单独计数，回复里能看到「坏行」
                    result.invalid += 1
                key = (str(row.get("code") or "").strip().upper(), fingerprint)
                if fingerprint and key[0]:
                    if key in seen:
                        keep = False
                        result.duplicated += 1
                    else:
                        seen.add(key)
                        if checks.get(key):
                            keep = False
                            result.existing += 1
                            if key[0] not in result.existing_codes and len(result.existing_codes) < 20:
                                result.existing_codes.append(key[0])
                if keep:
                    result.rows += 1
                    writer.writerow(row)

        result.checked = bool(seen_keys) and result.skipped_reason != "预检失败"

    if not (result.normalized or result.duplicated or result.existing):
        # 一个字都没改：直接用原文件，别让 momo 吃一份多余的拷贝
        Path(tmp_path).unlink(missing_ok=True)
        return result

    result.path = Path(tmp_path)
    return result


def _run_lookups(
    to_check: dict[tuple[str, str], tuple[str, str]],
    result: PrepareResult,
    *,
    lookup: Lookup | None,
    workers: int,
    progress: Callable[[int, int], None] | None = None,
) -> dict[tuple[str, str], bool]:
    """并发执行预检；任一查询报错就整体放弃预检（结果交回 momo 自己判重）。"""
    if lookup is None or not to_check:
        return {}

    def check(key: tuple[str, str], link: str, site: str) -> bool:
        return bool(lookup(key[0], link, site))

    total = len(to_check)
    items = list(to_check.items())
    done = 0
    if progress is not None:
        _safe_progress(progress, 0, total)

    try:
        if workers > 1 and total > 1:
            with ThreadPoolExecutor(max_workers=max(1, min(workers, total))) as pool:
                futures = {
                    pool.submit(check, key, link, site): key
                    for key, (link, site) in items
                }
                checks: dict[tuple[str, str], bool] = {}
                for future in as_completed(futures):
                    checks[futures[future]] = bool(future.result())
                    done += 1
                    if progress is not None and (done % 25 == 0 or done == total):
                        _safe_progress(progress, done, total)
        else:
            checks = {}
            for key, (link, site) in items:
                checks[key] = check(key, link, site)
                done += 1
                if progress is not None and (done % 25 == 0 or done == total):
                    _safe_progress(progress, done, total)
    except Exception:  # noqa: BLE001 - 预检失败不影响导入
        logger.warning("magnet import precheck failed, skipping precheck", exc_info=True)
        result.skipped_reason = "预检失败"
        return {}

    if progress is not None and done < total:
        _safe_progress(progress, total, total)
    return checks


def _safe_progress(progress: Callable[[int, int], None], done: int, total: int) -> None:
    try:
        progress(done, total)
    except Exception:  # noqa: BLE001
        logger.debug("precheck progress callback failed", exc_info=True)
