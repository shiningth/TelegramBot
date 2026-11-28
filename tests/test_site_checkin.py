"""站点签到模块：配置解析、到点判断、Cookie 保留、结果分类。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

import pytest

from telegram_bot.config import SiteCheckinConfig, SiteCheckinSiteConfig, site_checkin_config_from_dict
from telegram_bot.modules.base import ModuleCard
from telegram_bot.modules.site_checkin import SiteCheckinModule, is_due
from telegram_bot.shared import sitecheckin
from telegram_bot.shared.sitecheckin import (
    SignResult,
    classify,
    parse_cookie,
    sign_site,
    signed_dates,
    _json_state,
    load_quiz_table,
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
    assert _json_state('{"ret":-2,"msg":"补签卡不足"}')[0] == "pending"
    assert _json_state("<html>签到成功</html>") is None


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_browser_method_uses_trawl_scrape_and_reads_second_page(monkeypatch):
    """method=browser：走 TRAWL /scrape，第一趟是 Turnstile 页，第二趟已签上。"""
    from telegram_bot.shared import sitecheckin as sitecheckin_module

    pages = [
        {
            "html": "<html>请耐心等待签到验证程序加载.....<div class='cf-turnstile'></div></html>",
            "captchasSolved": ["turnstile"],
            "statusCode": 200,
        },
        {"html": "<html>签到成功 本次签到获得 100 个魔力值</html>", "statusCode": 200},
    ]
    calls = []

    def fake_post(url, json=None, timeout=None, **kwargs):
        calls.append((url, json))
        return _FakeResponse(pages[min(len(calls) - 1, len(pages) - 1)])

    monkeypatch.setattr(sitecheckin_module.httpx, "post", fake_post)

    result = sign_site(
        url="https://ourbits.club/attendance.php",
        cookie="ourbits_jwt=abc",
        method="browser",
        flaresolverr="http://192.168.50.105:8193",
    )

    assert result.state == "signed"
    assert len(calls) == 2
    assert calls[0][0].endswith("/scrape")
    assert calls[0][1]["solveEmbeddedCaptchas"] is True
    assert calls[0][1]["cookies"] == [
        {"name": "ourbits_jwt", "value": "abc", "domain": "ourbits.club", "path": "/"}
    ]


def test_browser_method_reports_captcha_when_trawl_solves_nothing(monkeypatch):
    from telegram_bot.shared import sitecheckin as sitecheckin_module

    def fake_post(url, json=None, timeout=None, **kwargs):
        return _FakeResponse(
            {
                "html": "<html>请耐心等待签到验证程序加载.....<div class='cf-turnstile'></div></html>",
                "captchasSolved": [],
                "statusCode": 200,
            }
        )

    monkeypatch.setattr(sitecheckin_module.httpx, "post", fake_post)

    result = sign_site(
        url="https://ourbits.club/attendance.php",
        cookie="ourbits_jwt=abc",
        method="browser",
        flaresolverr="http://192.168.50.105:8193",
    )

    assert result.state == "captcha"
    assert "Turnstile 未通过" in result.detail


def test_browser_method_timeout_lands_in_pending(monkeypatch):
    """TRAWL 整趟超时不能判失败：签到请求可能已经在浏览器里发出去了。"""
    from telegram_bot.shared import sitecheckin as sitecheckin_module

    def fake_post(url, json=None, timeout=None, **kwargs):
        raise sitecheckin_module.httpx.TimeoutException("read timeout")

    monkeypatch.setattr(sitecheckin_module.httpx, "post", fake_post)

    result = sign_site(
        url="https://ourbits.club/attendance.php",
        cookie="ourbits_jwt=abc",
        method="browser",
        flaresolverr="http://192.168.50.105:8193",
    )

    assert result.state == "pending"
    assert "超时" in result.detail


def test_browser_method_verifies_after_timeout(monkeypatch):
    """第一趟超时、复核那趟看到已签到 → 直接算成功。"""
    from telegram_bot.shared import sitecheckin as sitecheckin_module

    calls = []

    def fake_post(url, json=None, timeout=None, **kwargs):
        calls.append(json)
        if len(calls) <= 2:
            raise sitecheckin_module.httpx.TimeoutException("read timeout")
        return _FakeResponse({"html": "<html>已经签到过了，请勿重复刷新</html>", "statusCode": 200})

    monkeypatch.setattr(sitecheckin_module.httpx, "post", fake_post)

    result = sign_site(
        url="https://ourbits.club/attendance.php",
        cookie="ourbits_jwt=abc",
        method="browser",
        flaresolverr="http://192.168.50.105:8193",
    )

    assert result.state == "already"
    assert "复核" in result.detail


def test_json_state_treats_retroactive_card_shortage_as_pending():
    assert _json_state('{"ret":-5,"msg":"补签卡不足"}')[0] == "pending"



# ---------------------------------------------------------------- 答题式签到
QUIZ_HTML = """<html><body><form action='attendance.php' method='post'>
<img src='/pic/attend/2026-11-15/abc.jpg'/>
<label><input type='radio' name='ban_robot' value='2026-11-15 00:02:02.510673&17'>棋士</label>
<label><input type='radio' name='ban_robot' value='2026-11-15 00:02:02.510673&18'>灵笼</label>
<input type='submit' name='submit' value='提交'>
</form></body></html>"""

QUIZ_OK = "<html><body>签到成功，这是您的第 807 次签到，已连续签到 2 天，本次签到获得 105 个魔力值。</body></html>"


class _Resp:
    def __init__(self, text, url="https://tjupt.org/attendance.php", content=b"", status=200):
        self.text = text
        self.content = content
        self.url = url
        self.status_code = status


def test_load_quiz_table_handles_missing_and_bad_files(tmp_path):
    assert load_quiz_table(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_quiz_table(bad) == {}
    good = tmp_path / "good.json"
    good.write_text('{"posters": {"ABC": " 棋士 "}}', encoding="utf-8")
    assert load_quiz_table(good) == {"abc": "棋士"}


def test_quiz_options_and_pick():
    from telegram_bot.shared.sitecheckin import _quiz_options, _quiz_pick

    options = _quiz_options(QUIZ_HTML)
    assert [o[2] for o in options] == ["棋士", "灵笼"]
    picked = _quiz_pick(options, "灵 笼")
    assert picked is not None and picked[1].endswith("&18")


def test_quiz_method_answers_by_poster_hash(tmp_path, monkeypatch):
    poster = b"\xff\xd8fakejpeg"
    md5 = hashlib.md5(poster).hexdigest()
    table = tmp_path / "site_checkin_quiz.json"
    table.write_text(json.dumps({"posters": {md5: "棋士"}}), encoding="utf-8")

    posts = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            if "/pic/attend/" in url:
                return _Resp("", url=url, content=poster)
            return _Resp(QUIZ_HTML)

        def post(self, url, data=None, headers=None):
            posts.append(data)
            return _Resp(QUIZ_OK)

    monkeypatch.setattr(sitecheckin.httpx, "Client", FakeClient)
    result = sign_site(
        url="https://tjupt.org/attendance.php",
        cookie="access_token=abc",
        method="quiz",
        quiz_table=str(table),
    )
    assert result.state == "signed"
    assert posts and posts[0]["ban_robot"].endswith("&17")


def test_quiz_method_skips_unknown_poster_without_guessing(tmp_path, monkeypatch):
    table = tmp_path / "site_checkin_quiz.json"
    table.write_text(json.dumps({"posters": {"deadbeef": "棋士"}}), encoding="utf-8")
    posts = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            if "/pic/attend/" in url:
                return _Resp("", url=url, content=b"another-poster")
            return _Resp(QUIZ_HTML)

        def post(self, url, data=None, headers=None):  # pragma: no cover - 不该被调用
            posts.append(data)
            return _Resp(QUIZ_OK)

    monkeypatch.setattr(sitecheckin.httpx, "Client", FakeClient)
    result = sign_site(
        url="https://tjupt.org/attendance.php",
        cookie="access_token=abc",
        method="quiz",
        quiz_table=str(table),
    )
    assert result.state == "unknown"
    assert "题库" in result.detail
    assert posts == []

def test_get_method_passes_slider_then_reads_signed_page(monkeypatch):
    slider = "<html><body><div id='dragContainer'></div><script src='/slide_check_ab.js'></script></body></html>"
    signed = "<html><body><h2>签到成功</h2>这是您的第 1211 次签到，本次签到获得 1000 个魔力值。</body></html>"
    calls = {"n": 0}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            calls["n"] += 1
            return _Resp(slider if calls["n"] == 1 else signed)

        def post(self, url, data=None, headers=None):  # pragma: no cover
            raise AssertionError("method=get 不应提交表单")

    monkeypatch.setattr(sitecheckin.httpx, "Client", FakeClient)
    monkeypatch.setattr(sitecheckin, "_solve_slider", lambda *a, **k: "")
    result = sign_site(url="https://pt.0ff.cc/attendance.php", cookie="a=b", method="get")
    assert result.state == "signed"
    assert calls["n"] == 2

def test_retroactive_post_is_refused_to_save_cards(monkeypatch):
    page = "<html><body><form method='post' action='attendance.php'><input type='hidden' name='x' value='1'></form></body></html>"

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            return _Resp(page)

        def post(self, url, data=None, headers=None):  # pragma: no cover
            raise AssertionError("不应调用补签接口")

    monkeypatch.setattr(sitecheckin.httpx, "Client", FakeClient)
    result = sign_site(
        url="https://pt.0ff.cc/attendance.php",
        cookie="a=b",
        method="post",
        sign_url="https://pt.0ff.cc/ajax.php",
        post_data="action=attendanceRetroactive&params[date]={date}",
    )
    assert result.state == "failed"
    assert "补签卡" in result.detail

def test_get_retries_on_5xx_then_recovers(monkeypatch):
    calls = {"n": 0}
    page = "<html><body><h2>签到成功</h2>这是您的第 1212 次签到，本次签到获得 1000 个魔力值。</body></html>"

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                return _Resp("<html>error</html>", status=500)
            return _Resp(page)

        def post(self, url, data=None, headers=None):  # pragma: no cover
            raise AssertionError("不应提交")

    monkeypatch.setattr(sitecheckin.httpx, "Client", FakeClient)
    monkeypatch.setattr(sitecheckin.time, "sleep", lambda *_: None)
    result = sign_site(url="https://pt.0ff.cc/attendance.php", cookie="a=b", method="get")
    assert result.state == "signed"
    assert calls["n"] == 3


def test_get_reports_5xx_after_retries(monkeypatch):
    calls = {"n": 0}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            calls["n"] += 1
            return _Resp("<html>error</html>", status=500)

        def post(self, url, data=None, headers=None):  # pragma: no cover
            raise AssertionError("不应提交")

    monkeypatch.setattr(sitecheckin.httpx, "Client", FakeClient)
    monkeypatch.setattr(sitecheckin.time, "sleep", lambda *_: None)
    result = sign_site(url="https://pt.0ff.cc/attendance.php", cookie="a=b", method="get")
    assert result.state == "failed"
    assert "500" in result.detail and "稍后会自动再试" in result.detail
    assert calls["n"] == 4

def test_is_due_retries_failed_site_same_day(monkeypatch):
    site = SiteCheckinSiteConfig(id="x", name="X", url="https://x/", cron="1 0 * * *")
    now = datetime(2026, 11, 17, 9, 30)
    last = datetime(2026, 11, 17, 0, 1)
    assert is_due(site, now, last, last_state="signed") is False
    assert is_due(site, now, last, last_state="failed", attempts_today=1, retry_minutes=20) is True
    assert is_due(site, now, last, last_state="failed", attempts_today=3, retry_minutes=20) is False
    soon = datetime(2026, 11, 17, 0, 5)
    assert is_due(site, soon, last, last_state="failed", attempts_today=1, retry_minutes=20) is False
    # 滑块没过也要当天补试，否则 0 点那一刻的抖动会白白丢掉一整天
    assert is_due(site, now, last, last_state="slider", attempts_today=1, retry_minutes=20) is True


def test_solve_slider_retries_when_js_has_no_token(monkeypatch):
    page = '<script src="/slide_check_ab.js"></script>'
    js_without = "var x = 1;"
    js_with = 'const requestUrl = "https://pt.0ff.cc/set_access_token-aa-bb-YWNs-";'
    state = {"js": 0, "token": 0, "pages": 0}

    class FakeClient:
        def get(self, url, **kwargs):
            if "slide_check" in url:
                state["js"] += 1
                return _Resp(js_without if state["js"] == 1 else js_with)
            if "set_access_token" in url:
                state["token"] += 1
                return _Resp("")
            state["pages"] += 1
            return _Resp(page)

    monkeypatch.setattr(sitecheckin.time, "sleep", lambda *_: None)
    problem = sitecheckin._solve_slider(FakeClient(), page, "https://pt.0ff.cc/attendance.php")
    assert problem == ""
    assert state["js"] == 2 and state["token"] == 1


def test_slider_failure_falls_back_to_browser(monkeypatch):
    page = "<html><body><div id='dragContainer'></div></body></html>"

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            return _Resp(page)

        def post(self, url, data=None, headers=None):  # pragma: no cover
            raise AssertionError("should not post")

    def fake_browser(result, started, **kwargs):
        result.state = "already"
        result.detail = "今日已签（浏览器兜底）"
        return result

    monkeypatch.setattr(sitecheckin.httpx, "Client", FakeClient)
    monkeypatch.setattr(sitecheckin, "_solve_slider", lambda *a, **k: "滑块脚本里没有找到验证地址")
    monkeypatch.setattr(sitecheckin, "_sign_via_browser", fake_browser)
    result = sign_site(
        url="https://pt.0ff.cc/attendance.php",
        cookie="a=b",
        method="get",
        flaresolverr="http://192.168.50.105:8193",
    )
    assert result.state == "already"
    assert "浏览器兜底" in result.detail


def test_bonus_amount_reads_bonus_header():
    html = '<span class="color_bonus">魔力值 </span>[<a href="mybonus.php">使用</a>]: 626,858.3 &nbsp;(签到已得105)'
    assert sitecheckin._bonus_amount(html) == "105"


def test_already_detail_shows_bonus_instead_of_repeat_hint():
    html = "<div>(签到已得105)</div><div>您今天已经签到过了，请勿重复刷新。</div>"
    result = SignResult(state="already", detail="您今天已经签到过了，请勿重复刷新。", debug_html=html)
    sitecheckin._finish(result, __import__("time").monotonic())
    assert result.bonus == "105"
    assert "105" in result.detail
    assert "请勿重复" not in result.detail


def test_signed_detail_appends_bonus():
    html = "<div>签到成功，获得 100 个魔力值</div>"
    result = SignResult(state="signed", detail="签到成功", debug_html=html)
    sitecheckin._finish(result, __import__("time").monotonic())
    assert result.bonus == "100"
    assert "100" in result.detail


def test_record_counts_consecutive_days(tmp_path):
    from datetime import timedelta

    module = make_module(tmp_path)
    today = datetime.now().date()
    module._state["logs"] = [
        {"at": (today - timedelta(days=1)).isoformat() + "T08:00:00", "site": "ourbits", "state": "signed"},
        {"at": (today - timedelta(days=2)).isoformat() + "T08:00:00", "site": "ourbits", "state": "already"},
    ]
    site = module.config.sites[0]
    result = SignResult(state="signed", detail="签到成功 · 获得 100 个魔力值", bonus="100")
    entry = module._record(site, result)
    assert entry["streak"] == 3
    assert "连续 3 天" in entry["detail"]


def test_first_already_of_day_is_shown_as_success(tmp_path):
    module = make_module(tmp_path)
    site = module.config.sites[0]
    first = module._record(
        site, SignResult(state="already", detail="今日已签到 · 本次获得 105 个魔力值", bonus="105")
    )
    assert first["detail"].startswith("签到成功")
    assert "105" in first["detail"]
    second = module._record(
        site, SignResult(state="already", detail="今日已签到 · 本次获得 105 个魔力值", bonus="105")
    )
    assert second["detail"].startswith("今日已签到")


def test_turnstile_payload_reads_token_and_hidden_fields():
    html = (
        "<form method='POST' id='attendance'>"
        "<input type='hidden' name='userId' id='myUserId' value='55922'>"
        "<input type='hidden' name='cf-turnstile-response' value='tok-abc'>"
        "</form>"
    )
    payload = sitecheckin._turnstile_payload(html)
    assert payload["cf-turnstile-response"] == "tok-abc"
    assert payload["userId"] == "55922"


def test_turnstile_payload_is_empty_without_token():
    assert sitecheckin._turnstile_payload('<div class="cf-turnstile" data-sitekey="x"></div>') == {}


def test_browser_solves_turnstile_then_posts_form_itself(monkeypatch):
    solved_html = (
        "<form method='POST' id='attendance'>"
        "<input type='hidden' name='userId' value='55922'>"
        "<input type='hidden' name='cf-turnstile-response' value='tok-abc'>"
        "</form>"
    )
    signed_html = (
        "<table><tr><td class='text' id='stdmsg'>签到成功，这是您的第 1201 次签到，"
        "已连续签到 4 天，本次签到获得 25 个魔力值</td></tr></table>"
    )
    seen = {}

    def fake_scrape(url, endpoint, cookie, timeout, solve=True):
        return {
            "html": solved_html,
            "captchasSolved": ["turnstile"],
            "statusCode": 200,
            "url": url,
            "userAgent": "UA/1.0",
        }

    def fake_submit(url, html, cookie, user_agent, **kwargs):
        seen["html"] = html
        seen["ua"] = user_agent
        return signed_html

    monkeypatch.setattr(sitecheckin, "_trawl_scrape", fake_scrape)
    monkeypatch.setattr(sitecheckin, "_submit_turnstile_form", fake_submit)
    result = SignResult(state="unknown", detail="", http=0)
    out = sitecheckin._sign_via_browser(
        result,
        0.0,
        url="https://ourbits.club/attendance.php",
        cookie="ourbits_jwt=x",
        endpoint="http://127.0.0.1:8193",
        timeout=30,
        success_keywords="",
        already_keywords="",
        today="2026-11-24",
    )
    assert out.state == "signed"
    assert "1201" in out.detail
    assert seen["html"] == solved_html
    assert seen["ua"] == "UA/1.0"


def test_is_due_complex_cron_does_not_repeat_within_same_minute() -> None:
    site = SiteCheckinSiteConfig(id="x", name="X", url="https://x/", cron="10 8 * * 1-5")
    now = datetime(2026, 11, 13, 8, 10, 45)
    assert is_due(site, now, None) is True
    assert is_due(site, now, datetime(2026, 11, 13, 8, 10, 5)) is False
    assert is_due(site, datetime(2026, 11, 16, 8, 10, 5), datetime(2026, 11, 13, 8, 10, 5)) is True


def test_sign_site_sets_login_state_and_blocks_cross_origin_slider(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            if "slide_check" in url:
                return _Resp('const u = "https://evil.example/set_access_token-abc";')
            return _Resp("<a href='login.php'>请先登录</a>")

    monkeypatch.setattr(sitecheckin.httpx, "Client", FakeClient)
    login_res = sign_site(url="https://pt.0ff.cc/attendance.php", cookie="a=b", method="get")
    assert login_res.state == "login"

    slider_err = sitecheckin._solve_slider(
        FakeClient(),
        '<script src="/slide_check_1.js"></script>',
        "https://pt.0ff.cc/attendance.php",
    )
    assert "跨站" in slider_err


def test_quiz_network_error_does_not_raise(monkeypatch) -> None:
    class FailingClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            raise sitecheckin.httpx.ConnectError("network down")

    monkeypatch.setattr(sitecheckin.httpx, "Client", FailingClient)
    res = sign_site(url="https://tjupt.org/attendance.php", cookie="a=b", method="quiz")
    assert res.state == "failed"


def test_site_checkin_update_config_preserves_notify_token_and_retry_fields(tmp_path) -> None:
    module = SiteCheckinModule(
        make_card(),
        SiteCheckinConfig(
            enabled=True,
            notify_bot_token="12345:keep-secret",
            retry_failed_minutes=25,
            max_attempts_per_day=5,
            state_store=str(tmp_path / "state.json"),
        ),
        project_root=tmp_path,
    )
    module.update_config({"notify_bot_token": "", "notify_chat_id": "999"})
    assert module.config.notify_bot_token == "12345:keep-secret"
    assert module.config.retry_failed_minutes == 25
    assert module.config.max_attempts_per_day == 5


