from __future__ import annotations

import logging
import time
from pathlib import Path

from telegram_bot.config import LogConfig
from telegram_bot.logging_setup import configure_logging, prune_old_logs, shutdown_logging


def test_prune_old_logs_removes_files_past_keep_days(tmp_path: Path) -> None:
    fresh = tmp_path / "telegram-bot.log"
    stale = tmp_path / "telegram-bot.log.3"
    fresh.write_text("fresh", encoding="utf-8")
    stale.write_text("stale", encoding="utf-8")
    old = time.time() - 40 * 86400
    import os

    os.utime(stale, (old, old))

    removed = prune_old_logs(tmp_path, 30)

    assert removed == ["telegram-bot.log.3"]
    assert fresh.exists() and not stale.exists()


def test_prune_old_logs_skips_when_disabled(tmp_path: Path) -> None:
    stale = tmp_path / "old.log"
    stale.write_text("x", encoding="utf-8")
    import os

    old = time.time() - 400 * 86400
    os.utime(stale, (old, old))

    assert prune_old_logs(tmp_path, 0) == []
    assert stale.exists()


def test_configure_logging_reports_keep_days(tmp_path: Path) -> None:
    config = LogConfig(dir=str(tmp_path), file_name="t.log", keep_days=7)
    buffer = configure_logging(config, tmp_path)
    try:
        assert any("keep_days=7" in item["message"] for item in buffer.tail(20))
        assert (tmp_path / "t.log").exists()
    finally:
        shutdown_logging()
        logging.getLogger()  # 保证 handler 已清理
