"""站点签到模块：配置解析、到点判断、Cookie 保留、结果分类。"""

from __future__ import annotations

from datetime import datetime

import pytest

from telegram_bot.config import SiteCheckinConfig, SiteCheckinSiteConfig, site_checkin_config_from_dict
from telegram_bot.modules.base import ModuleCard
from telegram_bot.modules.site_checkin import SiteCheckinModule, is_due
from telegram_bot.shared.sitecheckin import (
    SignResult,
    classify,
    parse_cookie,
    signed_dates,
    _json_state,
)


def make_card() -> ModuleCard:
    return ModuleCard(id="site_checkin", name="站点签到", description="", category="site")


def make_module(tmp_path, **overrides) -> SiteCheckinModule:
    config = SiteCheckinConfig(
        enabled=True,
        sites=[
            SiteCheckinSiteConfig(id="ourbits", name="OurBits", url="https://example.com/attendance.php", cookie="a=1;b=2")
        ],
        state_store=str(tmp_path / "site_checkin_state.json"),
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return SiteCheckinModule(make_card(), config, project_root=tmp_path)


def test_site_config_parses_sites_and_drops_empty_url():
    config = site_checkin_config_from_dict(
        {
            "log_keep_days": 7,
            "sites": [
                {"id": "a", "name": "A", "url": "https://a.example/attendance.php", "cookie": "x=1"},
                {"name": "没有地址"},
            ],
        }
    )
    assert config.log_keep_days == 7
    assert [site.id for site in config.sites] == ["a"]
    assert config.sites[0].cron == "10 8 * * *"


def test_update_config_keeps_existing_cookie_when_blank(tmp_path):
    module = make_module(tmp_path)
    module.update_config(
        {
            "enabled": True,
            "sites": [{"id": "ourbits", "name": "OurBits 改名", "url": "https://example.com/attendance.php", "cookie": ""}],
        }
    )
    assert module.config.sites[0].cookie == "a=1;b=2"
    assert module.config.sites[0].name == "OurBits 改名"


def test_update_config_replaces_cookie_when_provided(tmp_path):
    module = make_module(tmp_path)
    module.update_config(
        {"sites": [{"id": "ourbits", "name": "OurBits", "url": "https://example.com/attendance.php", "cookie": "c=3"}]}
    )
    assert module.config.sites[0].cookie == "c=3"


def test_card_hides_cookie_values(tmp_path):
    module = make_module(tmp_path)
    card = module.card.config
    assert card["sites"][0]["has_cookie"] is True
    assert "cookie" not in card["sites"][0]


def test_is_due_runs_once_after_daily_time():
    site = SiteCheckinSiteConfig(id="s", name="S", url="https://e.example/a", cron="10 8 * * *")
    assert is_due(site, datetime(2026, 11, 12, 7, 59), None) is False
    assert is_due(site, datetime(2026, 11, 12, 8, 10), None) is True
    assert is_due(site, datetime(2026, 11, 12, 23, 30), None) is True
    assert is_due(site, datetime(2026, 11, 12, 23, 30), datetime(2026, 11, 12, 8, 10)) is False
    assert is_due(site, datetime(2026, 11, 13, 8, 11), datetime(2026, 11, 12, 8, 10)) is True


def test_classify_detects_common_states():
    assert classify("<div>抱歉 您今天已经签到过了</div>", final_url="", success_keywords="", already_keywords="")[0] == "already"
    assert classify("<div>签到成功，获得 20 个魔力值</div>", final_url="", success_keywords="", already_keywords="")[0] == "signed"
    assert classify("<div>滑动认证</div><div id='dragContainer'></div>", final_url="", success_keywords="", already_keywords="")[0] == "slider"
    assert classify("<a href='login.php'>登录</a>", final_url="", success_keywords="", already_keywords="")[0] == "login"


def test_classify_prefers_slider_over_success_text():
    html = "<div id='dragContainer'>滑动认证</div><script>var t='签到成功';</script>"
    state, _ = classify(html, final_url="", success_keywords="", already_keywords="")
    assert state == "slider"


def test_parse_cookie_handles_spaces_and_empty_parts():
    cookies = parse_cookie(" a=1; b=2 ; ;c=3 ")
    assert cookies == {"a": "1", "b": "2", "c": "3"}


def test_state_file_records_logs_and_trims(tmp_path):
    module = make_module(tmp_path, log_keep_days=1)
    site = module.config.sites[0]
    module._record(site, SignResult(state="signed", detail="签到成功", http=200, elapsed_ms=120))
    module._state["logs"].insert(
        1,
        {"at": "2020-01-01T00:00:00", "time": "01-01 00:00", "site": site.id, "state": "failed", "detail": "旧记录"},
    )
    module._save_state()
    logs = module.log_entries()
    assert len(logs) == 1
    assert logs[0]["state"] == "signed"


@pytest.mark.asyncio
async def test_run_now_unknown_site_returns_error(tmp_path):
    module = make_module(tmp_path)
    result = await module.run_now("不存在")
    assert result["ok"] is False
    assert "站点" in result["error"]


def test_handle_message_returns_ignored_with_module_id(tmp_path):
    import asyncio

    module = make_module(tmp_path)
    result = asyncio.run(module.handle_message(object()))

    assert result.status == 'ignored'
    assert result.module_id == 'site_checkin'


def test_signed_dates_reads_calendar_events():
    html = '<script>let events = JSON.parse(\'[{"start":"2026-11-13","title":25}]\')</script>'
    assert signed_dates(html) == {"2026-11-13"}


def test_signed_page_without_today_marker_is_downgraded(tmp_path):
    """页面回显「签到成功」但日历里没有今天 → 不能算签上。"""
    module = make_module(tmp_path)
    site = module.config.sites[0]
    result = SignResult(
        state="signed",
        detail="签到成功 这是您的第 1201 次签到",
        dates=("2026-11-12",),
    )
    refined = module._refine_already(site, result)
    assert refined.state == "unknown"
    assert "未见今日" in refined.detail


def test_same_day_same_count_with_today_marker_is_already(tmp_path):
    module = make_module(tmp_path)
    site = module.config.sites[0]
    today = datetime.now().strftime("%Y-%m-%d")
    module._state["last"] = {
        site.id: {"at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "count": 1201}
    }
    result = SignResult(state="signed", detail="签到成功 第 1201 次签到", dates=(today,))
    refined = module._refine_already(site, result)
    assert refined.state == "already"
    assert "日历已标记今日" in refined.detail


def test_json_state_maps_ajax_replies():
    assert _json_state('{"ret":-1,"msg":"已经签到"}') == ("already", "已经签到")
    assert _json_state('{"ret":0,"msg":"签到成功"}')[0] == "signed"
    assert _json_state('{"ret":-2,"msg":"补签卡不足"}')[0] == "failed"
    assert _json_state("<html>签到成功</html>") is None

