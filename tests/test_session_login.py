from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from telegram_bot.app import TelegramBotApplication
from telegram_bot.config import AppConfig
from telegram_bot.shared.session_login import (
    SessionLoginManager,
    mask_phone,
    normalize_phone,
    safe_session_name,
)


# ---------------------------------------------------------------- 纯函数
def test_normalize_phone_adds_country_code() -> None:
    assert normalize_phone("13800138000") == "+8613800138000"
    assert normalize_phone("+8613800138000") == "+8613800138000"
    assert normalize_phone("0086 138-0013-8000") == "+8613800138000"


@pytest.mark.parametrize("value", ["", "abc", "+86", "861380013800"])
def test_normalize_phone_rejects_bad_values(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_phone(value)


def test_safe_session_name_rejects_traversal() -> None:
    assert safe_session_name("ltx_user") == "ltx_user"
    assert safe_session_name("user-2") == "user-2"
    for bad in ("", "../etc", "a/b", "名字"):
        with pytest.raises(ValueError):
            safe_session_name(bad)


def test_mask_phone_keeps_head_and_tail() -> None:
    assert mask_phone("+8613800138000") == "+861****00"


# ---------------------------------------------------------------- 管理器
def test_state_lists_session_files(tmp_path: Path) -> None:
    (tmp_path / "ltx_user.session").write_bytes(b"x" * 20)
    (tmp_path / "user_session.session").write_bytes(b"y" * 30)
    (tmp_path / "ltx_user.session-journal").write_bytes(b"")
    manager = SessionLoginManager(tmp_path)

    state = manager.state()

    names = sorted(item["name"] for item in state["sessions"])
    assert names == ["ltx_user", "user_session"]
    assert state["pending"] == []
    assert state["sessions_dir"] == str(tmp_path)


def test_state_masks_pending_phone_and_keeps_stage_label(tmp_path: Path) -> None:
    manager = SessionLoginManager(tmp_path)
    manager._tickets["abc"] = {
        "ticket": "abc",
        "phone": "+8613800138000",
        "session_name": "ltx_user",
        "stage": "code_sent",
        "detail": "已发送",
        "phone_code_hash": "h",
        "client": None,
        "created_at": 1e12,  # 未来时间，避免被清理
    }

    state = manager.state()

    assert state["pending"][0]["phone"] == "+861****00"
    assert state["pending"][0]["stage_label"] == "等待验证码"
    assert "13800138000" not in str(state)


def test_prune_drops_expired_tickets(tmp_path: Path) -> None:
    manager = SessionLoginManager(tmp_path, ticket_ttl=0.0)
    manager._tickets["old"] = {
        "ticket": "old",
        "phone": "+8613800138000",
        "session_name": "ltx_user",
        "stage": "code_sent",
        "detail": "",
        "phone_code_hash": "h",
        "client": None,
        "created_at": 0.0,
    }

    state = manager.state()

    assert state["pending"] == []


@pytest.mark.asyncio
async def test_sign_in_unknown_ticket_explains_expiry(tmp_path: Path) -> None:
    manager = SessionLoginManager(tmp_path)
    with pytest.raises(ValueError, match="登录请求已过期"):
        await manager.sign_in(ticket="nope", code="12345")


@pytest.mark.asyncio
async def test_cancel_unknown_ticket_is_ok(tmp_path: Path) -> None:
    manager = SessionLoginManager(tmp_path)
    assert (await manager.cancel(ticket="nope"))["ok"] is True


@pytest.mark.asyncio
async def test_send_code_requires_credentials(tmp_path: Path) -> None:
    manager = SessionLoginManager(tmp_path)
    with pytest.raises(ValueError, match="api_id"):
        await manager.send_code(phone="+8613800138000", session_name="ltx_user", api_id=0, api_hash="")


class _BoomClient:
    """send_code_request 直接炸，模拟被 Telegram 限流。"""

    def __init__(self, path: str, api_id: int, api_hash: str, **kwargs: Any) -> None:
        self.path = Path(path)

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"sqlite-ish")

    async def send_code_request(self, phone: str) -> Any:  # noqa: ARG002
        raise RuntimeError("A wait of 55531 seconds is required")

    async def disconnect(self) -> None:
        return None


@pytest.mark.asyncio
async def test_failed_send_code_leaves_no_session_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import telethon

    monkeypatch.setattr(telethon, "TelegramClient", _BoomClient)
    manager = SessionLoginManager(tmp_path)
    with pytest.raises(RuntimeError, match="55531"):
        await manager.send_code(phone="+8613800138000", session_name="zz_test", api_id=30255931, api_hash="a" * 32)
    assert list(tmp_path.glob("*.session")) == []
    assert manager.state()["sessions"] == []


def test_state_ignores_in_progress_temp_sessions(tmp_path: Path) -> None:
    (tmp_path / "ltx_user.session").write_bytes(b"x" * 10)
    (tmp_path / "ltx_user.deadbeef0001.session").write_bytes(b"x" * 10)
    names = [row["name"] for row in SessionLoginManager(tmp_path).sessions()]
    assert names == ["ltx_user"]


# ---------------------------------------------------------------- app 接线
def _make_app(tmp_path: Path) -> TelegramBotApplication:
    app = object.__new__(TelegramBotApplication)
    app.config = AppConfig()
    app.config_path = tmp_path / "config.json"
    app.loop = None
    app.modules = {}
    app.session_login = SessionLoginManager(tmp_path / "sessions")
    return app


def test_session_login_state_reports_api_ready(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    state = app.session_login_state()
    assert state["ok"] is True
    assert state["api_ready"] is False
    assert state["in_use"] == []

    app.config.telegram.api_id = 30255931
    app.config.telegram.api_hash = "0123456789abcdef0123456789abcdef"
    assert app.session_login_state()["api_ready"] is True


def test_login_actions_require_running_loop(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    for call in (app.start_session_login, app.submit_session_code, app.submit_session_password, app.cancel_session_login):
        result: dict[str, Any] = call({"phone": "+8613800138000", "session_name": "ltx_user"})
        assert result["ok"] is False
        assert "服务未在运行" in result["error"]
