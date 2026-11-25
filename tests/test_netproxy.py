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
from telegram_bot.config import AppConfig, network_config_from_dict
from telegram_bot.shared import netproxy


class _FakeAdapter:
    def __init__(self) -> None:
        self.connected = False
        self.last_error = ""
        self.started = 0

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "connected": self.connected,
            "session_name": "telegram_bot",
            "username": "",
            "monitored_groups": [],
            "last_error": self.last_error,
        }

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        pass


def _make_app(tmp_path: Path) -> TelegramBotApplication:
    app = object.__new__(TelegramBotApplication)
    app.config = AppConfig()
    app.config_path = tmp_path / "config.json"
    app.telegram = _FakeAdapter()
    app.loop = None
    return app


@pytest.fixture(autouse=True)
def _reset_proxy() -> Any:
    netproxy.configure(None)
    yield
    netproxy.configure(None)


# ---------------------------------------------------------------- 解析与脱敏


def test_parse_proxy_url_fills_default_ports() -> None:
    spec = netproxy.parse_proxy_url("127.0.0.1:7890")
    assert spec is not None
    assert spec["scheme"] == "http"
    assert spec["port"] == 7890
    socks = netproxy.parse_proxy_url("socks5://user:pw@10.0.0.2")
    assert socks is not None
    assert socks["port"] == 1080
    assert socks["username"] == "user"


def test_parse_proxy_url_rejects_empty() -> None:
    assert netproxy.parse_proxy_url("") is None
    assert netproxy.parse_proxy_url("http://") is None


def test_mask_proxy_url_hides_password() -> None:
    masked = netproxy.mask_proxy_url("socks5://user:secret@10.0.0.2:1080")
    assert "secret" not in masked
    assert masked == "socks5://user:***@10.0.0.2:1080"


# ---------------------------------------------------------------- 绕过判定


def test_everything_direct_when_disabled() -> None:
    netproxy.configure(AppConfig().network)
    assert netproxy.should_bypass("https://javdb.com") is True
    assert netproxy.telethon_proxy() is None
    client = netproxy.client_external(timeout=1)
    client.close()


def test_public_host_uses_proxy_when_enabled() -> None:
    cfg = AppConfig().network
    cfg.enabled = True
    cfg.proxy_url = "http://127.0.0.1:7890"
    netproxy.configure(cfg)
    assert netproxy.should_bypass("https://javdb.com/v/abc") is False
    assert netproxy.should_bypass("https://api.telegram.org") is False


def test_private_targets_always_bypass() -> None:
    cfg = AppConfig().network
    cfg.enabled = True
    cfg.proxy_url = "http://127.0.0.1:7890"
    netproxy.configure(cfg)
    for target in (
        "http://192.168.50.105:8066/api/magnets",
        "http://momo:8066/api/magnets",
        "http://127.0.0.1:8191/v1",
        "http://10.1.2.3:8193/scrape",
    ):
        assert netproxy.should_bypass(target) is True, target


def test_no_proxy_patterns_support_domains_and_cidr() -> None:
    cfg = AppConfig().network
    cfg.enabled = True
    cfg.proxy_url = "http://127.0.0.1:7890"
    cfg.no_proxy = ["example.com", ".corp", "203.0.113.0/24"]
    netproxy.configure(cfg)
    assert netproxy.should_bypass("https://example.com/x") is True
    assert netproxy.should_bypass("https://a.corp/x") is True
    assert netproxy.should_bypass("http://203.0.113.9/x") is True
    assert netproxy.should_bypass("https://example.org/x") is False


def test_telethon_proxy_returns_socks_tuple() -> None:
    cfg = AppConfig().network
    cfg.enabled = True
    cfg.proxy_url = "socks5://u:p@127.0.0.1:1080"
    netproxy.configure(cfg)
    proxy = netproxy.telethon_proxy()
    if proxy is None:  # 环境里没装 python-socks 时直接跳过
        pytest.skip("python-socks 未安装")
    assert proxy[1] == "127.0.0.1"
    assert proxy[2] == 1080
    assert proxy[3] is True
    assert proxy[4] == "u"


def test_telegram_scope_can_be_turned_off() -> None:
    cfg = AppConfig().network
    cfg.enabled = True
    cfg.proxy_url = "http://127.0.0.1:7890"
    cfg.proxy_for_telegram = False
    netproxy.configure(cfg)
    assert netproxy.telethon_proxy() is None


# ---------------------------------------------------------------- 配置解析


def test_network_config_from_empty_dict_defaults_to_direct() -> None:
    cfg = network_config_from_dict({})
    assert cfg.enabled is False
    assert cfg.proxy_url == ""
    assert "192.168.0.0/16" in cfg.no_proxy


def test_network_config_reads_lists_and_strings() -> None:
    cfg = network_config_from_dict({"enabled": True, "proxy_url": "http://127.0.0.1:7890", "no_proxy": "a.com, b.com"})
    assert cfg.no_proxy == ["a.com", "b.com"]
    assert cfg.active() is True


def test_network_config_uses_env_when_file_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TGBOT_PROXY_URL", "http://10.0.0.9:3128")
    monkeypatch.delenv("TGBOT_PROXY_ENABLED", raising=False)
    cfg = network_config_from_dict({})
    assert cfg.proxy_url == "http://10.0.0.9:3128"
    assert cfg.enabled is True


def test_network_config_env_can_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TGBOT_PROXY_URL", "http://10.0.0.9:3128")
    monkeypatch.setenv("TGBOT_PROXY_ENABLED", "false")
    cfg = network_config_from_dict({})
    assert cfg.enabled is False


# ---------------------------------------------------------------- 面板接口


def test_snapshot_masks_proxy_and_reports_scope(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app.config.network.enabled = True
    app.config.network.proxy_url = "socks5://user:secret@10.0.0.2:1080"
    snap = app.network_settings_snapshot()
    assert snap["has_proxy"] is True
    assert snap["active"] is True
    assert "secret" not in snap["proxy"]
    assert snap["proxy_for_telegram"] is True


def test_update_network_settings_saves_and_reconnects(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    result = app.update_network_settings({"network": {"enabled": True, "proxy_url": "http://127.0.0.1:7890"}})
    assert result["enabled"] is True
    assert "proxy_url" in result["changed"]
    assert app.config.network.proxy_url == "http://127.0.0.1:7890"
    assert netproxy.config() is app.config.network
    assert app.config_path.exists()


def test_update_network_settings_rejects_bad_url(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with pytest.raises(ValueError):
        app.update_network_settings({"proxy_url": "http://"})


def test_update_network_settings_keeps_masked_url(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app.config.network.proxy_url = "http://127.0.0.1:7890"
    result = app.update_network_settings({"proxy_url": "http://u:***@127.0.0.1:7890"})
    assert app.config.network.proxy_url == "http://127.0.0.1:7890"
    assert result["changed"] == []


def test_test_network_proxy_uses_saved_url_when_blank(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app.config.network.proxy_url = "http://127.0.0.1:7890"
    with patch.object(netproxy, "test_proxy", return_value={"ok": True}) as fake:
        app.test_network_proxy({"proxy_url": ""})
    fake.assert_called_once_with("http://127.0.0.1:7890")


def test_test_network_proxy_without_any_url(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    result = app.test_network_proxy({})
    assert result["ok"] is False


# ---------------------------------------------------------------- 出口对照


class _FakeResp:
    status_code = 200

    def json(self) -> dict[str, str]:
        return {"ip": "5.6.7.8", "country": "JP", "city": "Tokyo", "org": "AS2516 KDDI"}


class _FakeClient:
    def __init__(self, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def get(self, _url: str) -> _FakeResp:
        return _FakeResp()


def test_test_proxy_reports_changed_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(netproxy.httpx, "Client", _FakeClient)
    monkeypatch.setattr(netproxy, "direct_ip", lambda **_kwargs: "1.2.3.4")
    result = netproxy.test_proxy("http://127.0.0.1:7890")
    assert result["ok"] is True
    assert result["ip"] == "5.6.7.8"
    assert result["direct_ip"] == "1.2.3.4"
    assert result["egress_changed"] is True


def test_test_proxy_flags_unchanged_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(netproxy.httpx, "Client", _FakeClient)
    monkeypatch.setattr(netproxy, "direct_ip", lambda **_kwargs: "5.6.7.8")
    result = netproxy.test_proxy("http://127.0.0.1:7890")
    assert result["egress_changed"] is False


def test_direct_ip_returns_blank_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def __init__(self, **_kwargs: object) -> None:
            raise RuntimeError("no route")

    monkeypatch.setattr(netproxy.httpx, "Client", _Boom)
    assert netproxy.direct_ip() == ""
