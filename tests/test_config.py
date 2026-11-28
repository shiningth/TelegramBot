from __future__ import annotations

from pathlib import Path

import pytest

from telegram_bot.config import AppConfig, SiteCheckinSiteConfig
from telegram_bot.modules.base import ModuleCard


def test_config_file_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    AppConfig.ensure_file(path)

    config = AppConfig.load(path)
    config.telegram.monitored_groups = ["-1001", "-1002"]
    config.telegram.monitored_group_meta = {
        "-1001": {"title": "主群", "username": "main", "kind": "group"},
        "-1002": {"title": "抽奖频道", "username": "lottery", "kind": "channel"},
    }
    config.save(path)

    loaded = AppConfig.load(path)
    assert loaded.telegram.monitored_groups == ["-1001", "-1002"]
    assert loaded.telegram.monitored_group_meta["-1001"]["title"] == "主群"
    assert loaded.telegram.monitored_group_meta["-1002"]["kind"] == "channel"


def test_ai_provider_type_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = AppConfig()
    config.ai.reply_provider.provider_type = "claude"
    config.ai.image_provider.provider_type = "openai-compatible"
    config.save(path)

    loaded = AppConfig.load(path)

    assert loaded.ai.reply_provider.provider_type == "claude"
    assert loaded.ai.image_provider.provider_type == "openai-compatible"


def test_interrupted_config_replace_keeps_previous_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.json"
    config = AppConfig()
    config.save(path)
    original = path.read_bytes()
    config.project_name = "updated"

    def fail_replace(source, destination):
        assert source.parent == destination.parent
        raise OSError("simulated replace failure")

    with monkeypatch.context() as context:
        context.setattr("telegram_bot.config.os.replace", fail_replace)
        with pytest.raises(OSError):
            config.save(path)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_setting_and_module_snapshots_only_report_secret_presence() -> None:
    config = AppConfig()
    config.network.proxy_url = "http://proxy-user:proxy-secret@localhost:8080"
    config.checkin.notify_bot_token = "check-in-secret"
    config.ltx_watch.notify_bot_token = "collector-secret"
    config.site_checkin.notify_bot_token = "site-notify-secret"
    site = SiteCheckinSiteConfig(id="test", cookie="site-cookie", headers="Authorization: Key")
    config.site_checkin.sites = [site]

    snapshot = config.public_dict()
    assert snapshot["network"]["proxy_url"] == "" and snapshot["network"]["has_proxy"]
    assert snapshot["checkin"]["has_notify_bot_token"]
    assert snapshot["ltx_watch"]["has_notify_bot_token"]
    assert snapshot["site_checkin"]["has_notify_bot_token"]
    assert snapshot["site_checkin"]["sites"][0]["has_cookie"]
    assert snapshot["site_checkin"]["sites"][0]["has_headers"]
    card = ModuleCard("test", "test", "", "test", config={
        "notify_bot_token": config.site_checkin.notify_bot_token,
        "sites": [{"cookie": site.cookie}],
    })
    public_card = card.public_dict()
    assert public_card["config"]["notify_bot_token"] == ""
    assert public_card["config"]["sites"][0] == {"cookie": "", "has_cookie": True}
    assert card.config["notify_bot_token"] == config.site_checkin.notify_bot_token
    for secret in ("proxy-user", "proxy-secret", "check-in-secret", "collector-secret", "site-notify-secret", "site-cookie", site.headers):
        assert secret not in str(snapshot) and secret not in str(public_card)


def test_send_red_packet_minimums_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = AppConfig()
    config.send_red_packet.min_packet_amount = 2
    config.send_red_packet.strict_min_packet_amount = 101
    config.send_red_packet.strict_min_packet_group_ids = ["-1000000000001"]
    config.save(path)

    loaded = AppConfig.load(path)

    assert loaded.send_red_packet.min_packet_amount == 2
    assert loaded.send_red_packet.strict_min_packet_amount == 101
    assert loaded.send_red_packet.strict_min_packet_group_ids == ["-1000000000001"]


def test_chat_games_config_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = AppConfig()
    config.modules.chat_games = True
    config.chat_games.lucky_word.words = ["咖啡"]
    config.chat_games.emoji_riddle.reward_text = "+250"
    config.chat_games.garbled_decode.command_prefixes = ["/decode"]
    config.save(path)

    loaded = AppConfig.load(path)

    assert loaded.modules.chat_games is True
    assert loaded.chat_games.lucky_word.words == ["咖啡"]
    assert loaded.chat_games.emoji_riddle.reward_text == "+250"
    assert loaded.chat_games.garbled_decode.command_prefixes == ["/decode"]


def test_user_profile_config_roundtrip_and_clamps(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = AppConfig.from_dict(
        {
            "modules": {"user_profile": True},
            "user_profile": {
                "command_prefixes": ["/who"],
                "default_sample_size": 999,
                "min_sample_size": 5,
                "max_sample_size": 120,
                "min_required_samples": 4,
                "timeout_seconds": 900,
                "max_tokens": 99999,
                "style_prompt": "温和但有梳理感",
            },
        }
    )
    config.save(path)

    loaded = AppConfig.load(path)

    assert loaded.modules.user_profile is True
    assert loaded.user_profile.command_prefixes == ["/who"]
    assert loaded.user_profile.default_sample_size == 120
    assert loaded.user_profile.max_sample_size == 120
    assert loaded.user_profile.min_required_samples == 4
    assert loaded.user_profile.timeout_seconds == 600
    assert loaded.user_profile.max_tokens == 12000
    assert loaded.user_profile.style_prompt == "温和但有梳理感"


def test_user_profile_sample_limit_supports_two_thousand() -> None:
    config = AppConfig.from_dict(
        {
            "user_profile": {
                "default_sample_size": 9999,
                "max_sample_size": 9999,
            }
        }
    )

    assert config.user_profile.default_sample_size == 2000
    assert config.user_profile.max_sample_size == 2000


def test_chat_history_config_roundtrip_and_clamps(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = AppConfig.from_dict(
        {
            "chat_history": {
                "enabled": True,
                "database_path": "runtime/custom/chat.sqlite3",
                "max_text_length": 999999,
                "backfill_enabled": True,
                "backfill_batch_size": 999999,
            }
        }
    )
    config.save(path)

    loaded = AppConfig.load(path)

    assert loaded.chat_history.enabled is True
    assert loaded.chat_history.database_path == "runtime/custom/chat.sqlite3"
    assert loaded.chat_history.max_text_length == 65536
    assert loaded.chat_history.backfill_enabled is True
    assert loaded.chat_history.backfill_batch_size == 5000


def test_user_profile_defaults_include_fx_all_aliases() -> None:
    config = AppConfig()

    assert "/fx" in config.user_profile.command_prefixes
    assert ".fx" in config.user_profile.command_prefixes
    assert "毒舌" in config.user_profile.style_prompt
    assert "适度夸张" in config.user_profile.style_prompt


def test_env_overrides_keep_secret_strings_as_strings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.json"
    AppConfig().save(path)
    monkeypatch.setenv("TGBOT_ADMIN_USERNAME", "00123")
    monkeypatch.setenv("TGBOT_ADMIN_PASSWORD", "true")
    monkeypatch.setenv("TGBOT_TELEGRAM_BOT_TOKEN", "123456:true")
    monkeypatch.setenv("TGBOT_ADMIN_PORT", "9090")
    monkeypatch.setenv("TGBOT_TELEGRAM_ENABLED", "yes")

    loaded = AppConfig.load(path)
    assert loaded.admin.username == "00123"
    assert isinstance(loaded.admin.username, str)
    assert loaded.admin.password == "true"
    assert isinstance(loaded.admin.password, str)
    assert loaded.telegram.bot_token == "123456:true"
    assert loaded.admin.port == 9090
    assert loaded.telegram.enabled is True
