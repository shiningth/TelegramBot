from __future__ import annotations

import json
import time
from pathlib import Path

from telegram_bot.usage import build_ai_usage_stats


def test_build_ai_usage_stats_all_range_uses_actual_time_span(tmp_path: Path) -> None:
    usage_file = tmp_path / "ai_usage.jsonl"
    now = time.time()
    rows = [
        {"ts": now - 6000, "feature": "ai_chat", "model": "gpt-4o", "success": True, "total_tokens": 1000},
        {"ts": now - 60, "feature": "ai_chat", "model": "gpt-4o", "success": True, "total_tokens": 500},
    ]
    usage_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    stats = build_ai_usage_stats(usage_file, "all")
    assert stats["summary"]["requests"] == 2
    assert stats["summary"]["total_tokens"] == 1500
    assert stats["summary"]["rpm"] < 0.1
    assert stats["summary"]["tpm"] < 20.0
