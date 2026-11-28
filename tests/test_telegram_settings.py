from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from telegram_bot.app import TelegramBotApplication
from telegram_bot.config import AppConfig
from telegram_bot.shared.tgcheck import check_formats

FAKE_API_ID = 12345678
FAKE_API_HASH = "0123456789abcdef0123456789abcdef"
FAKE_BOT_USERNAME = "example_test_bot"
FAKE_BOT_TOKEN = "123456789:" + "TestToken" + "0" * 26


class _FakeAdapter:
    def __init__(self, *, connected: bool = False, username: str = "", error: str = "") -> None:
        self.connected = connected
        self.username = username
        self.last_error = error
        self.started = 0
        self.stopped = 0

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "connected": self.connected,
            "session_name": "telegram_bot",
            "username": self.username,
            "monitored_groups": [],
            "last_error": self.last_error,
        }

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


def _make_app(tmp_path: Path, adapter: _FakeAdapter | None = None) -> TelegramBotApplication:
    config = AppConfig()
    config_path = tmp_path / "config.json"
    app = object.__new__(TelegramBotApplication)
    app.config = config
    app.config_path = config_path
    app.telegram = adapter or _FakeAdapter()
    app.loop = None
    return app


def test_snapshot_reports_masked_state(tmp_path: Path) -> None:
    app = _make_app(tmp_path, _FakeAdapter(connected=True, username=FAKE_BOT_USERNAME))
    app.config.telegram.enabled = True
    app.config.telegram.api_id = FAKE_API_ID
    app.config.telegram.api_hash = FAKE_API_HASH
    app.config.telegram.bot_token = FAKE_BOT_TOKEN

    snapshot = app.telegram_settings_snapshot()

    assert snapshot["enabled"] is True
    assert snapshot["api_id"] == FAKE_API_ID
    assert snapshot["has_api_hash"] is True
    assert snapshot["has_bot_token"] is True
    assert snapshot["connected"] is True
    assert snapshot["bot_username"] == FAKE_BOT_USERNAME
    # 快照只暴露布尔，不回显明文
    assert "0123456789abcdef" not in str(snapshot)
    assert "TestToken" not in str(snapshot)


def test_update_keeps_secrets_when_masked_values_are_echoed_back(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app.config.telegram.api_hash = FAKE_API_HASH
    app.config.telegram.bot_token = FAKE_BOT_TOKEN

    with patch.object(app, "_schedule_telegram_restart") as restart:
        result = app.update_telegram_settings(
            {
                "telegram": {
                    "api_id": str(FAKE_API_ID),
                    "api_hash": "012***def",
                    "bot_token": "123***000",
                    "enabled": True,
                }
            }
        )

    assert result["changed"] == ["api_id", "enabled"]
    assert app.config.telegram.api_hash == FAKE_API_HASH
    assert app.config.telegram.bot_token.startswith("123456789:")
    restart.assert_called_once()
    assert app.config_path.exists()


def test_update_replaces_secret_when_a_new_value_is_given(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app.config.telegram.api_hash = "oldhasholdhasholdhasholdhash01"

    with patch.object(app, "_schedule_telegram_restart"):
        result = app.update_telegram_settings({"telegram": {"api_hash": "newhashnewhashnewhashnewhash02"}})

    assert result["changed"] == ["api_hash"]
    assert app.config.telegram.api_hash == "newhashnewhashnewhashnewhash02"


def test_update_without_changes_skips_restart(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app.config.telegram.api_id = FAKE_API_ID

    with patch.object(app, "_schedule_telegram_restart") as restart:
        result = app.update_telegram_settings({"telegram": {"api_id": str(FAKE_API_ID), "api_hash": "", "bot_token": ""}})

    assert result["changed"] == []
    assert result["restarting"] is False
    restart.assert_not_called()


def test_update_rejects_non_numeric_api_id(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with pytest.raises(ValueError):
        app.update_telegram_settings({"telegram": {"api_id": "abc123"}})


def test_test_settings_reports_format_errors_without_network(tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    with patch("telegram_bot.app.check_api_credentials") as api_check, patch(
        "telegram_bot.app.check_bot_token"
    ) as token_check:
        result = app.test_telegram_settings({"telegram": {"api_id": "12ab", "api_hash": "short", "bot_token": "nope"}})

    assert result["ok"] is False
    names = {item["name"]: item["ok"] for item in result["checks"]}
    assert names["api_id 格式"] is False
    assert names["api_hash 格式"] is False
    assert names["bot token 格式"] is False
    api_check.assert_not_called()
    token_check.assert_not_called()


def test_test_settings_falls_back_to_stored_secrets(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app.config.telegram.api_id = FAKE_API_ID
    app.config.telegram.api_hash = FAKE_API_HASH
    app.config.telegram.bot_token = FAKE_BOT_TOKEN

    with patch("telegram_bot.app.check_api_credentials", return_value={"name": "api_id / api_hash 可用", "ok": True, "detail": "ok"}) as api_check, patch(
        "telegram_bot.app.check_bot_token", return_value={"name": "bot token 有效", "ok": True, "detail": "@bot"}
    ) as token_check:
        result = app.test_telegram_settings({"telegram": {"api_hash": "012***def", "bot_token": ""}})

    assert result["ok"] is True
    api_check.assert_called_once_with(FAKE_API_ID, FAKE_API_HASH)
    token_check.assert_called_once_with(FAKE_BOT_TOKEN)


def test_check_formats_accepts_real_looking_credentials() -> None:
    checks = check_formats(str(FAKE_API_ID), FAKE_API_HASH, FAKE_BOT_TOKEN)
    assert all(item["ok"] for item in checks)
    assert [item["name"] for item in checks] == ["api_id 格式", "api_hash 格式", "bot token 格式"]
