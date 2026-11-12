from __future__ import annotations

import logging
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LogConfig


class PruningRotatingFileHandler(RotatingFileHandler):
    """滚动时顺手删掉超过 keep_days 的旧日志文件（按天保留，不无限增长）。"""

    def __init__(self, *args, keep_days: int = 30, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.keep_days = max(int(keep_days or 0), 0)

    def doRollover(self) -> None:  # noqa: N802 - 系统方法名
        super().doRollover()
        prune_old_logs(Path(self.baseFilename).parent, self.keep_days)


def prune_old_logs(log_dir: Path, keep_days: int) -> list[str]:
    """删除 mtime 超过 keep_days 的日志文件，返回被删掉的文件名。"""
    if keep_days <= 0 or not log_dir.exists():
        return []
    deadline = time.time() - keep_days * 86400
    removed: list[str] = []
    for path in log_dir.iterdir():
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < deadline:
                path.unlink()
                removed.append(path.name)
        except OSError:
            continue
    return removed


class RecentLogBuffer(logging.Handler):
    def __init__(self, capacity: int = 300) -> None:
        super().__init__()
        self.capacity = capacity
        self.records: list[dict[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        item = {
            "time": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        self.records.append(item)
        if len(self.records) > self.capacity:
            self.records = self.records[-self.capacity :]

    def tail(self, limit: int = 100) -> list[dict[str, str]]:
        return self.records[-limit:]


def configure_logging(config: LogConfig, project_root: Path) -> RecentLogBuffer:
    log_dir = _resolve_path(project_root, config.dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, config.level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    file_path = log_dir / config.file_name
    backup_count = max(config.max_files - 1, 0)
    file_handler = PruningRotatingFileHandler(
        file_path,
        maxBytes=config.max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        keep_days=config.keep_days,
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    removed = prune_old_logs(log_dir, config.keep_days)
    if removed:
        logging.getLogger(__name__).info("清理过期日志：%s", ", ".join(removed))

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    recent = RecentLogBuffer()
    recent.setFormatter(formatter)
    root.addHandler(recent)

    # Telethon 在网络抖动时会每秒刷几条重连日志，两小时能刷掉 5MB 滚动文件，
    # 把真正有用的业务日志挤掉。这里把网络层压到 ERROR，只保留致命问题。
    for name, level in (
        ("telethon.network.mtprotosender", logging.ERROR),
        ("telethon.network.connection.connection", logging.ERROR),
        ("telethon.client.updates", logging.WARNING),
    ):
        logging.getLogger(name).setLevel(level)

    logging.getLogger(__name__).info(
        "Logging ready: path=%s max_bytes=%s max_files=%s keep_days=%s",
        file_path,
        config.max_bytes,
        config.max_files,
        config.keep_days,
    )
    return recent


def shutdown_logging() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


def list_log_files(config: LogConfig, project_root: Path) -> list[dict[str, object]]:
    log_dir = _resolve_path(project_root, config.dir)
    if not log_dir.exists():
        return []
    files = sorted(
        [path for path in log_dir.iterdir() if path.is_file()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return [
        {
            "name": path.name,
            "size": path.stat().st_size,
            "modified": path.stat().st_mtime,
        }
        for path in files[: config.max_files]
    ]


def read_log_tail(config: LogConfig, project_root: Path, name: str, lines: int = 300) -> str:
    log_dir = _resolve_path(project_root, config.dir)
    path = (log_dir / name).resolve()
    if log_dir.resolve() not in path.parents and path != log_dir.resolve():
        raise ValueError("Invalid log file path")
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(name)
    with path.open("r", encoding="utf-8", errors="replace") as fp:
        content = fp.readlines()
    return "".join(content[-lines:])


def _resolve_path(project_root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return project_root / path
