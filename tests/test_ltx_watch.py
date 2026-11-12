from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from telegram_bot.config import AppConfig, LtxWatchConfig, MagnetImportConfig, TelegramConfig, ltx_watch_config_from_dict
from telegram_bot.modules.base import ModuleCard
from telegram_bot.modules.ltx_watch import LtxWatchModule, parse_message

CHANNEL_TEXT = (
    "🎬 高衩賽車女郎的過激誘惑 君川結衣  #LTX #IPZ-998 #98T\n"
    "ed2k://|file|98T@IPZ-998_LTX.mp4|7558373792|E3BBC5FA2423C96ADDD3133DB2A5C3FE|/"
)


def test_parse_message_extracts_all_fields() -> None:
    item = parse_message(
        CHANNEL_TEXT,
        message_id=1940,
        date=datetime(2026, 11, 1, 7, 33),
        buttons=["https://javdb.com/v/rwYJ"],
    )
    assert item is not None
    assert item.code == "IPZ-998"
    assert item.size_text == "7.04 GB"
    assert item.magnet_hash == "e3bbc5fa2423c96addd3133db2a5c3fe"
    assert item.javdb_url == "https://javdb.com/v/rwYJ"
    assert item.title.startswith("高衩賽車女郎")
    assert "#LTX" not in item.title and "ed2k" not in item.title
    assert item.published_at == datetime(2026, 11, 1, 7, 33)


def test_parse_message_handles_glued_and_digit_prefix_codes() -> None:
    glued = "🎬 彼女の姉 #LTX #FSDSS-615 #98Ted2k://|file|98T@FSDSS-615_LTX.mp4|7896213100|35cb7a090d83792511c3c27cf9b6770d|/"
    assert parse_message(glued, message_id=1).code == "FSDSS-615"

    digit = "🎬 ラグジュTV 1887#LTX #259LUXU-1900ed2k://|file|259LUXU-1900_LTX.mp4|1000|aa" + "b" * 30 + "|/"
    item = parse_message(digit, message_id=2)
    # 番号必须以字母开头：站点前缀 259 丢掉
    assert item is not None and item.code == "LUXU-1900"

    # 没有 #标签 时回退到文件名
    no_tag = "MIDA-138 真っ白美ボディ#自制ed2k://|file|MIDA-138_LTX.mp4|5580257603|55b0dd98" + "c" * 24 + "|/"
    assert parse_message(no_tag, message_id=3).code == "MIDA-138"


def test_parse_message_rejects_text_without_ed2k() -> None:
    assert parse_message("公告：今晚维护", message_id=9) is None
    assert parse_message("", message_id=10) is None


def test_small_file_uses_mb() -> None:
    item = parse_message(
        "标题 #ABC-123 ed2k://|file|ABC-123.mp4|157286400|" + "d" * 32 + "|/",
        message_id=11,
    )
    assert item is not None and item.size_text == "150 MB"


def _message(mid: int, text: str, date: datetime | None = None, buttons: list[str] | None = None):
    return SimpleNamespace(
        id=mid,
        message=text,
        date=date or datetime(2026, 11, 1, 8, 0),
        buttons=[[SimpleNamespace(url=url)] for url in (buttons or [])],
    )


class FakeClient:
    def __init__(self, messages):
        self.messages = messages

    def is_connected(self) -> bool:
        return True

    async def get_entity(self, channel):
        return SimpleNamespace(id=4419698318, title="Jasna LTX 频道")

    async def get_messages(self, entity, min_id=0, limit=100):
        return [m for m in self.messages if m.id > min_id]


def make_module(tmp_path: Path, messages, *, dry_run: bool = True, lookup=None, meta=None, channel: str = "testchannel"):
    calls: list[dict] = []

    def importer(base, user, password, rows, **kwargs):
        calls.append({"rows": rows, **kwargs})
        return {"success": True, "data": {"imported_records": len(rows), "updated_records": 0, "skipped_records": 0}}

    def default_lookup(*args, **kwargs):
        return None

    def default_meta(url, **kwargs):
        return {"author": "君川結衣", "cover_url": "https://c0.jdbstatic.com/covers/rw/rwYJ.jpg"}

    card = ModuleCard("ltx_watch", "LTX 频道自动导入", "", "automation", enabled=True)
    module = LtxWatchModule(
        card,
        LtxWatchConfig(dry_run=dry_run, initial_full_scan=True, channel=channel),
        MagnetImportConfig(api_password="secret"),
        TelegramConfig(api_id=1, api_hash="hash"),
        tmp_path,
        importer=importer,
        lookup=lookup or default_lookup,
        meta_fetcher=meta or default_meta,
    )
    module._client = FakeClient(messages)
    module.importer_calls = calls
    # 默认不真连 JavDB 搜索页；需要时由用例显式替换 module._search
    module._search = lambda code, **kwargs: None
    return module


def test_dry_run_parses_without_writing(tmp_path: Path) -> None:
    module = make_module(tmp_path, [_message(1940, CHANNEL_TEXT, buttons=["https://javdb.com/v/rwYJ"])])
    result = asyncio.run(module._tick())
    assert result["would_import"] == 1
    assert result["imported"] == 0
    assert module.importer_calls == []
    state = json.loads((tmp_path / "runtime/data/ltx_watch_state.json").read_text(encoding="utf-8"))
    assert state["processed"]["1940"]["status"] == "dry-run"
    assert state["last_message_id"] == 1940
    assert module.card.metrics["dry_run"] is True


def test_real_import_writes_row_with_author_and_cover(tmp_path: Path) -> None:
    module = make_module(
        tmp_path,
        [_message(1940, CHANNEL_TEXT, buttons=["https://javdb.com/v/rwYJ"])],
        dry_run=False,
    )
    asyncio.run(module._tick())
    assert len(module.importer_calls) == 1
    row = module.importer_calls[0]["rows"][0]
    assert row["code"] == "IPZ-998"
    assert row["size"] == "7.04 GB"
    assert row["author"] == "君川結衣"
    assert json.loads(row["magnets"])[0].startswith("ed2k://|file|98T@IPZ-998")
    assert json.loads(row["images"]) == ["https://c0.jdbstatic.com/covers/rw/rwYJ.jpg"]
    assert row["source_site"] == "ltx"
    assert row["forum_id"] == row["forum_type"] == row["forum_name"] == "LTX破解"
    # 保留消息的时分秒，不再截断成 00:00:00
    assert row["published_at"].startswith("2026-11-01 ")
    assert not row["published_at"].endswith("00:00:00")
    assert row["magnet_hash"] == "e3bbc5fa2423c96addd3133db2a5c3fe"


def test_existing_in_momo_is_skipped(tmp_path: Path) -> None:
    seen: list[dict] = []

    def lookup(*args, **kwargs):
        seen.append(kwargs)
        return {"id": 529956, "code": "IPZ-998"}

    module = make_module(tmp_path, [_message(1940, CHANNEL_TEXT)], dry_run=False, lookup=lookup)
    result = asyncio.run(module._tick())
    assert result["skipped"] == 1
    assert module.importer_calls == []
    assert seen and seen[0]["source_site"] == "ltx"


def test_samples_carry_skip_reason(tmp_path: Path) -> None:
    module = make_module(
        tmp_path,
        [_message(1940, CHANNEL_TEXT)],
        dry_run=False,
        lookup=lambda *a, **k: {"id": 529956, "code": "IPZ-998"},
    )
    asyncio.run(module._tick())
    sample = module.card.metrics["samples"][0]
    assert sample["status"] == "skipped"
    assert sample["reason"] == "momo 已存在同一 ed2k"


def test_missing_author_does_not_call_javdb_search(tmp_path: Path) -> None:
    """用户要求别在补演员上耗时间：fetch_meta 没给演员就空着，不再单独搜 JavDB。"""

    searched: list[str] = []

    def search(code, **kwargs):
        searched.append(code)
        return {"author": "蒼井なぎさ"}

    module = make_module(
        tmp_path,
        [_message(1940, CHANNEL_TEXT, buttons=["https://javdb.com/v/rwYJ"])],
        dry_run=False,
        meta=lambda url, **kwargs: {"cover_url": "https://pics.dmm.co.jp/digital/video/ipzz00998/ipzz00998pl.jpg"},
    )
    module._search = search
    asyncio.run(module._tick())
    row = module.importer_calls[0]["rows"][0]
    assert row["author"] == ""
    assert searched == []


def test_metadata_failure_imports_without_cover_and_records_it(tmp_path: Path) -> None:
    module = make_module(
        tmp_path,
        [_message(1940, CHANNEL_TEXT, buttons=["https://javdb.com/v/rwYJ"])],
        dry_run=False,
        meta=lambda url, **kwargs: None,
    )
    module._search = lambda code, **kwargs: None
    asyncio.run(module._tick())
    row = module.importer_calls[0]["rows"][0]
    assert row["author"] == ""
    assert json.loads(row["images"]) == []
    state = json.loads((tmp_path / "runtime/data/ltx_watch_state.json").read_text(encoding="utf-8"))
    # 缺封面只记一笔，之后用 scripts/repair_missing_covers.py 补
    assert state["missing_cover"] == ["IPZ-998"]


def test_unparsable_message_is_skipped_once(tmp_path: Path) -> None:
    module = make_module(tmp_path, [_message(2000, "公告：今晚维护")], dry_run=False)
    asyncio.run(module._tick())
    asyncio.run(module._tick())
    assert module.importer_calls == []
    state = json.loads((tmp_path / "runtime/data/ltx_watch_state.json").read_text(encoding="utf-8"))
    assert state["processed"]["2000"]["status"] == "skipped"


def test_config_defaults_and_parsing() -> None:
    config = AppConfig.from_dict(AppConfig.defaults_dict())
    assert config.ltx_watch.dry_run is True
    assert config.ltx_watch.channel == ""
    assert config.ltx_watch.backfill_stop_after == 0
    assert config.ltx_watch.session_name == "ltx_user"
    assert config.ltx_watch.notify_enabled is False
    assert config.ltx_watch.notify_cron == "0 21 * * *"
    assert config.modules.ltx_watch is False

    parsed = ltx_watch_config_from_dict({"channel": "@examplechan", "poll_interval_seconds": 1, "max_pending_days": 999})
    assert parsed.channel == "examplechan"
    assert parsed.poll_interval_seconds == 60
    assert parsed.max_pending_days == 90


class NotifyClient(FakeClient):
    def __init__(self, messages=None):
        super().__init__(messages or [])
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, target, text):
        self.sent.append((target, text))


def test_notify_sends_summary_when_cron_matches(tmp_path: Path) -> None:
    from datetime import datetime as _dt

    module = make_module(tmp_path, [], dry_run=False)
    now = _dt.now()
    module.config.notify_enabled = True
    module.config.notify_cron = f"{now.minute} {now.hour} * * *"
    module.config.notify_target = "me"
    module._state["window"] = {"since": "2026-10-30 10:00:00", "imported": 12, "skipped": 3, "failed": 1}
    module._state["last_notify_at"] = "2026-10-30 10:00:00"
    module._state["missing_cover"] = ["IPZ-998"]
    client = NotifyClient()
    module._client = client

    asyncio.run(module._maybe_notify())
    assert len(client.sent) == 1
    target, text = client.sent[0]
    assert target == "me"
    assert "新增磁链 12 条" in text and "跳过重复 3 条" in text and "待补封面 1 条" in text
    assert module._state["last_notify_at"] != "2026-10-30 10:00:00"
    assert module._state["window"]["imported"] == 0

    # 同一分钟内不应重复发送
    asyncio.run(module._maybe_notify())
    assert len(client.sent) == 1


def test_notify_is_skipped_when_disabled_or_not_due(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime as _dt

    from telegram_bot.modules import ltx_watch as lw

    module = make_module(tmp_path, [], dry_run=False)
    client = NotifyClient()
    module._client = client

    # 关闭时即使到点也不发
    module.config.notify_cron = "0 21 * * *"
    module.config.notify_enabled = False
    module._state["last_notify_at"] = "2026-10-30 10:00:00"

    class Noon(_dt):
        @classmethod
        def now(cls, tz=None):
            return _dt(2026, 11, 3, 12, 0, 0)

    monkeypatch.setattr(lw, "datetime", Noon)
    asyncio.run(module._maybe_notify())
    assert client.sent == []

    # 开启但还没到 21:00，也不发
    module.config.notify_enabled = True
    asyncio.run(module._maybe_notify())
    assert client.sent == []


def test_switching_to_live_mode_resets_dry_run_records(tmp_path: Path) -> None:
    module = make_module(tmp_path, [_message(1940, CHANNEL_TEXT)], dry_run=True)
    asyncio.run(module._tick())
    state = json.loads((tmp_path / "runtime/data/ltx_watch_state.json").read_text(encoding="utf-8"))
    assert state["processed"]["1940"]["status"] == "dry-run"
    assert state["last_message_id"] == 1940

    module.update_config({"dry_run": False})
    state = json.loads((tmp_path / "runtime/data/ltx_watch_state.json").read_text(encoding="utf-8"))
    assert "1940" not in state["processed"]
    assert state["last_message_id"] == 0
    assert module.config.dry_run is False

def test_notify_test_sends_without_resetting_window(tmp_path: Path) -> None:
    module = make_module(tmp_path, [], dry_run=False)
    module._state["window"] = {"since": "2026-10-30 10:00:00", "imported": 5, "skipped": 1, "failed": 0}
    client = NotifyClient()
    module._client = client

    result = asyncio.run(module.notify_test())
    assert result["ok"] is True
    assert len(client.sent) == 1
    assert "新增磁链 5 条" in client.sent[0][1]
    assert module._state["window"]["imported"] == 5


def test_samples_carry_message_time(tmp_path: Path) -> None:
    module = make_module(tmp_path, [_message(1940, CHANNEL_TEXT, date=datetime(2026, 11, 1, 21, 30))], dry_run=True)
    asyncio.run(module._tick())
    sample = module.card.metrics["samples"][0]
    assert sample["msg_time"].startswith("2026-11-01")

def test_bot_token_uses_bot_api(tmp_path: Path) -> None:
    module = make_module(tmp_path, [], dry_run=False)
    module.config.notify_bot_token = "123:abc"
    module.config.notify_chat_id = "123456789"
    module._state["window"] = {"since": "2026-10-30 10:00:00", "imported": 7, "skipped": 0, "failed": 0}

    sent: list[str] = []
    module._send_via_bot_api = lambda text: sent.append(text)

    client = NotifyClient()
    module._client = client
    result = asyncio.run(module.notify_test())
    assert result["ok"] is True
    assert sent and sent[0].startswith("【测试】")
    assert client.sent == []


def test_plain_target_uses_text_mode(tmp_path: Path) -> None:
    module = make_module(tmp_path, [], dry_run=False)
    module.config.notify_target = "me"
    module._state["window"] = {"since": "2026-10-30 10:00:00", "imported": 7, "skipped": 0, "failed": 0}
    client = NotifyClient()
    module._client = client
    asyncio.run(module.notify_test())
    assert client.sent and client.sent[0][0] == "me"
def test_row_includes_cover_url(tmp_path: Path) -> None:
    module = make_module(tmp_path, [_message(1940, CHANNEL_TEXT, buttons=["https://javdb.com/v/rwYJ"])], dry_run=False)
    asyncio.run(module._tick())
    row = module.importer_calls[0]["rows"][0]
    assert row["cover_url"] == "https://c0.jdbstatic.com/covers/rw/rwYJ.jpg"
    assert json.loads(row["images"]) == [row["cover_url"]]
def test_metadata_delay_is_honoured(tmp_path: Path) -> None:
    module = make_module(tmp_path, [_message(1940, CHANNEL_TEXT, buttons=["https://javdb.com/v/rwYJ"])], dry_run=False)
    module.config.metadata_delay_seconds = 7
    slept: list[float] = []

    async def fake_sleep(delay):
        slept.append(delay)

    module._sleep = fake_sleep
    asyncio.run(module._tick())
    assert slept == [7]

def test_missing_channel_is_reported(tmp_path: Path) -> None:
    module = make_module(tmp_path, [], dry_run=False)
    module.config.channel = ""
    result = asyncio.run(module._tick())
    assert result.get("error") == "未配置监控频道"


def test_backfill_stops_after_consecutive_existing(tmp_path: Path) -> None:
    msgs = [_message(1900 + i, CHANNEL_TEXT) for i in range(5)]
    module = make_module(tmp_path, msgs, dry_run=False, lookup=lambda *a, **k: {"id": 1})
    module.config.backfill_stop_after = 2
    result = asyncio.run(module._tick())
    assert result.get("stopped_early") is True
    assert result["skipped"] == 2
    assert result["imported"] == 0
def test_magnet_fingerprint_ignores_filename() -> None:
    from telegram_bot.shared.magnetboard_api import magnet_fingerprint

    a = "ed2k://|file|www.98T.la@ABP-975-U-LTX.mp4|9211430568|2FF405C22171111111111111111111AA|/"
    b = "ed2k://|file|98T@ABP-975-U_LTX.mp4|9211430568|2FF405C22171111111111111111111AA|/"
    assert magnet_fingerprint(a) == magnet_fingerprint(b)
    assert magnet_fingerprint("magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01") == "abcdef0123456789abcdef0123456789abcdef01"
    assert magnet_fingerprint("") == ""

def test_metadata_source_order_is_fixed() -> None:
    from telegram_bot.config import LtxWatchConfig
    from telegram_bot.shared import javdb

    # 用户 2026-11-10 指定的顺序：DMM → AVbase → AVWikiDB → Jav321 → MGStage → JavDB
    assert javdb._META_ORDER == ("dmm", "avbase", "avwikidb", "jav321", "mgstage", "javdb")
    assert "javbus" not in javdb._META_ORDER
    # JavDB 现在部分页面要登录，默认不抓；要启用得手工把 "javdb" 写进配置
    assert "javdb" not in LtxWatchConfig().metadata_sources


def test_fetch_meta_asks_dmm_and_avbase_first(monkeypatch) -> None:
    """DMM 与 AVbase 每轮都跑（合并演员）；缺口才继续往慢源走。"""
    from telegram_bot.shared import javdb

    calls: list[str] = []

    def dmm(**_kwargs):
        calls.append("dmm")
        return {"cover_url": "https://pics.dmm.co.jp/digital/video/abc00123/abc00123pl.jpg", "author": "女優A"}

    def avbase(**_kwargs):
        calls.append("avbase")
        return {"cover_url": "", "author": "女優A 女優B"}

    def avwikidb(**_kwargs):
        calls.append("avwikidb")
        return {"cover_url": "", "author": "女優C"}

    def jav321(**_kwargs):
        calls.append("jav321")
        return {"cover_url": "", "author": "女優D"}

    monkeypatch.setattr(
        javdb,
        "_SOURCE_FUNCS",
        {"dmm": dmm, "avbase": avbase, "avwikidb": avwikidb, "jav321": jav321},
    )
    data = javdb.fetch_meta("", code="ABC-123")
    assert calls == ["dmm", "avbase"]
    assert data is not None
    assert data["cover_url"].endswith("abc00123pl.jpg")
    assert data["author"] == "女優A 女優B"
    assert data["source"] == "dmm"


def test_fetch_meta_falls_back_to_slow_sources_when_gap(monkeypatch) -> None:
    from telegram_bot.shared import javdb

    calls: list[str] = []

    def dmm(**_kwargs):
        calls.append("dmm")
        return {"cover_url": "https://pics.dmm.co.jp/x.jpg", "author": ""}

    def jav321(**_kwargs):
        calls.append("jav321")
        return None

    def avbase(**_kwargs):
        calls.append("avbase")
        return None

    def avwikidb(**_kwargs):
        calls.append("avwikidb")
        return {"cover_url": "", "author": "女優A"}

    monkeypatch.setattr(
        javdb,
        "_SOURCE_FUNCS",
        {"dmm": dmm, "jav321": jav321, "avbase": avbase, "avwikidb": avwikidb},
    )
    data = javdb.fetch_meta("", code="ABC-123")
    assert calls == ["dmm", "avbase", "avwikidb"]
    assert data is not None and data["author"] == "女優A"
    assert data["cover_url"].endswith("/x.jpg")


def test_fetch_meta_single_source_when_complete(monkeypatch) -> None:
    from telegram_bot.shared import javdb

    calls: list[str] = []

    def fake(**_kwargs):
        calls.append("x")
        return {"cover_url": "https://pics.dmm.co.jp/x.jpg", "author": "女優B"}

    monkeypatch.setattr(javdb, "_SOURCE_FUNCS", {"dmm": fake, "jav321": fake})
    data = javdb.fetch_meta("", code="ABC-123")
    assert calls == ["x"]
    assert data is not None and data["author"] == "女優B"


def test_jav321_parses_actress_and_cover() -> None:
    from telegram_bot.shared import javdb

    class Resp:
        status_code = 200
        text = (
            '<b>出演者</b>: <a href="/star/1/1">神菜美まい</a> &nbsp; <br>'
            '<img src="https://pics.dmm.co.jp/digital/video/ipx00879/ipx00879pl.jpg">'
        )

    class Client:
        def get(self, url, headers=None, timeout=None):
            assert url.endswith("/video/ipx00879")
            return Resp()

    data = javdb._fetch_jav321(code="IPX-879", client=Client())
    assert data is not None
    assert data["author"] == "神菜美まい"
    assert data["cover_url"].endswith("ipx00879pl.jpg")
    assert data["source"] == "jav321"


def test_avwikidb_parses_actress_and_cover(monkeypatch) -> None:
    from telegram_bot.shared import javdb

    page = (
        "<p>女優は 神菜美まい｜タイトル</p>"
        '<img src="https://pics.dmm.co.jp/digital/video/ipx00879/ipx00879pl.jpg">'
    )
    monkeypatch.setattr(javdb, "fetch_via_flaresolverr", lambda url, endpoint, **kwargs: page)
    data = javdb._fetch_avwikidb(code="IPX-879", flaresolverr="http://192.0.2.10:8191")
    assert data is not None
    assert data["author"] == "神菜美まい"
    assert data["cover_url"].endswith("ipx00879pl.jpg")
    assert data["source"] == "avwikidb"


def test_dmm_cover_resolves_mono_cid_via_search() -> None:
    """ABF-165 这类 mono/dvd 作品在 DMM 的 cid 带厂商前缀（118abf165），靠搜索页才拿得到。"""
    from telegram_bot.shared import javdb

    class Resp:
        def __init__(self, code, content=b"", text=""):
            self.status_code = code
            self.content = content
            self.text = text

    class Client:
        def __init__(self):
            self.urls: list[str] = []

        def get(self, url, headers=None, timeout=None):
            self.urls.append(url)
            if "search/=" in url:
                return Resp(200, text='<a href="/mono/dvd/-/detail/=/cid=118abf165/">ABF-165</a>')
            if "mono/movie/adult/118abf165" in url:
                return Resp(200, content=b"x" * 90000)
            return Resp(302)

    client = Client()
    url = javdb.fetch_dmm_cover("ABF-165", client=client, timeout=5)
    assert url == "https://pics.dmm.co.jp/mono/movie/adult/118abf165/118abf165pl.jpg"
    assert any("searchstr=ABF165" in item for item in client.urls)


def test_dmm_meta_returns_author_from_search_page() -> None:
    """DMM 搜索页结果卡写「出演者：xxx」，所以一条请求就能拿到演员（约 0.7 秒）。"""
    from telegram_bot.shared import javdb

    class Resp:
        def __init__(self, code, content=b"", text=""):
            self.status_code = code
            self.content = content
            self.text = text

    class Client:
        def __init__(self):
            self.urls: list[str] = []

        def get(self, url, headers=None, timeout=None):
            self.urls.append(url)
            if "search/=" in url:
                return Resp(200, text='<p class="text-xs">出演者：miru</p>')
            if "pics.dmm.co.jp" in url:
                return Resp(200, content=b"x" * 70000)
            return Resp(404)

    client = Client()
    data = javdb._fetch_dmm(code="SSIS-932", client=client, timeout=5)
    assert data is not None
    assert data["author"] == "miru"
    assert data["cover_url"] == "https://pics.dmm.co.jp/digital/video/ssis00932/ssis00932pl.jpg"
    assert data["source"] == "dmm"


def test_fetch_meta_stops_at_dmm_when_cover_and_author_ready() -> None:
    from telegram_bot.shared import javdb

    class Resp:
        def __init__(self, code, content=b"", text=""):
            self.status_code = code
            self.content = content
            self.text = text

    class Client:
        def __init__(self):
            self.urls: list[str] = []

        def get(self, url, headers=None, timeout=None):
            self.urls.append(url)
            if "search/=" in url:
                return Resp(200, text="出演者：miru")
            if "pics.dmm.co.jp" in url:
                return Resp(200, content=b"x" * 70000)
            return Resp(404)

    client = Client()
    data = javdb.fetch_meta(
        "", code="SSIS-932", client=client, retries=1, sleep=lambda _s: None, sources=("dmm", "jav321")
    )
    assert data is not None
    assert data["author"] == "miru"
    assert data["source"] == "dmm"
    assert not any("jav321.com" in u for u in client.urls)


def test_avbase_parses_embedded_actors_json(monkeypatch) -> None:
    """AVbase 把演员放在页面内嵌 JSON：`"actors":[{"id":46092,"name":"miru",…}]`。"""
    from telegram_bot.shared import javdb

    page = (
        '<span>出演者登録なし</span>'
        '"actors":[{"id":46092,"name":"miru","order":null,'
        '"image_url":"https://pics.dmm.co.jp/mono/actjpgs/miru2.jpg","talent":{"id":67939}}]'
        '"cover_url":"https://pics.dmm.co.jp/digital/video/ssis00932/ssis00932pl.jpg"'
    )
    monkeypatch.setattr(javdb, "fetch_via_flaresolverr", lambda url, endpoint, **kwargs: page)
    data = javdb._fetch_avbase(code="SSIS-932", flaresolverr="http://192.0.2.10:8193")
    assert data is not None
    assert data["author"] == "miru"
    assert data["cover_url"].endswith("ssis00932pl.jpg")
    assert data["source"] == "avbase"


def test_avbase_falls_back_to_talent_link(monkeypatch) -> None:
    from telegram_bot.shared import javdb

    page = '<a class="card" href="/talents/%E6%A1%83%E8%B0%B7%E3%82%A8%E3%83%AA%E3%82%AB">x</a>'
    monkeypatch.setattr(javdb, "fetch_via_flaresolverr", lambda url, endpoint, **kwargs: page)
    data = javdb._fetch_avbase(code="MGSMPL-001", flaresolverr="http://192.0.2.10:8193")
    assert data is not None
    assert data["author"] == "桃谷エリカ"


def test_fetch_meta_uses_slow_source_for_actor_when_dmm_has_none(monkeypatch) -> None:
    """DMM 只出封面时，演员要由 AVbase / AVWikiDB 补上（用户 2026-11-10：慢点没关系）。"""
    from telegram_bot.shared import javdb

    calls: list[str] = []

    def fake_dmm(**kwargs):
        calls.append("dmm")
        return {"cover_url": "https://pics.dmm.co.jp/digital/video/ssis00932/ssis00932pl.jpg", "author": "", "source": "dmm"}

    def fake_avbase(**kwargs):
        calls.append("avbase")
        return {"cover_url": "", "author": "miru", "source": "avbase"}

    monkeypatch.setattr(javdb, "_SOURCE_FUNCS", {"dmm": fake_dmm, "avbase": fake_avbase})
    data = javdb.fetch_meta("", code="SSIS-932", sources=("dmm", "avbase"))
    assert data is not None
    assert data["author"] == "miru"
    assert data["cover_url"].endswith("ssis00932pl.jpg")
    assert data["source"] == "dmm"
    assert calls == ["dmm", "avbase"]


def test_dmm_code_normalisation() -> None:
    from telegram_bot.shared.javdb import dmm_code

    assert dmm_code("SSIS-858") == "ssis00858"
    assert dmm_code("IPZZ-965") == "ipzz00965"
    assert dmm_code("259LUXU-1839") == "259luxu1839"
    assert dmm_code("FC2-PPV-1234567") == "fc2ppv1234567"


def test_fetch_meta_uses_dmm_cover_and_jav321_author() -> None:
    """DMM 出封面、Jav321 出演员：前两源无货也能补齐，且不会再碰 JavDB。"""
    from telegram_bot.shared import javdb

    class Resp:
        def __init__(self, code, content=b"", text=""):
            self.status_code = code
            self.content = content
            self.text = text

    class Client:
        def __init__(self):
            self.urls = []

        def get(self, url, headers=None, timeout=None):
            self.urls.append(url)
            if "dmm.co.jp" in url:
                return Resp(200, content=b"x" * 70000)
            if "jav321.com" in url:
                return Resp(200, text='<b>出演者</b>: <a href="/star/1/1">女優A</a> &nbsp; <br>')
            return Resp(404)

    client = Client()
    data = javdb.fetch_meta(
        "", code="SSIS-858", client=client, retries=1, sleep=lambda _s: None, sources=("dmm", "jav321", "javdb")
    )
    assert data is not None
    assert data["cover_url"].endswith("ssis00858pl.jpg")
    assert data["author"] == "女優A"
    assert data["source"] == "dmm"
    assert any("jav321.com" in u for u in client.urls)
    assert not any("javdb.com" in u for u in client.urls)

def test_fetch_meta_prefers_flaresolverr(monkeypatch) -> None:
    from telegram_bot.shared import javdb

    calls = []

    def fake_fs(url, endpoint, **kwargs):
        calls.append((url, endpoint))
        return '<img class="video-cover" src="https://c0.jdbstatic.com/covers/ab/abcd.jpg">'

    monkeypatch.setattr(javdb, "fetch_via_flaresolverr", fake_fs)
    data = javdb.fetch_meta(
        "https://javdb.com/v/abc", code="ABC-123", flaresolverr="http://192.0.2.10:8191", sources=("javdb",)
    )
    assert data is not None
    assert data["cover_url"].endswith("abcd.jpg")
    assert calls[0][1] == "http://192.0.2.10:8191"
def test_code_variants_strip_site_prefix() -> None:
    from telegram_bot.shared.javdb import code_variants

    assert code_variants("259LUXU-1573") == ["259LUXU-1573", "LUXU-1573"]
    assert code_variants("200GANA-1131") == ["200GANA-1131", "GANA-1131"]
    assert code_variants("300MAAN-691") == ["300MAAN-691", "MAAN-691"]
    assert code_variants("336TNB-011") == ["336TNB-011", "TNB-011"]
    assert code_variants("SSIS-858") == ["SSIS-858"]
    assert code_variants("") == []
def test_lookup_tries_prefix_stripped_variant(tmp_path: Path) -> None:
    body = ("259LUXU-1006\n"
            "ed2k://|file|98T@259LUXU-1006_LTX.mp4|1234567|AABBCCDDEEFF00112233445566778899|/")
    module = make_module(tmp_path, [_message(1941, body)], dry_run=False)
    calls: list[str] = []

    def lookup(*args, **kwargs):
        calls.append(kwargs.get("code"))
        return {"id": 1} if kwargs.get("code") == "LUXU-1006" else None

    module._lookup = lookup
    result = asyncio.run(module._tick())
    # 解析阶段就把 259 前缀去掉，查重直接用 LUXU-1006
    assert calls == ["LUXU-1006"]
    assert result["skipped"] == 1
    assert result["imported"] == 0


def test_fc2_is_displayed_but_not_imported(tmp_path: Path) -> None:
    body = "FC2-PPV-1234567 素人作品\ned2k://|file|FC2-PPV-1234567_LTX.mp4|1234567|AABBCCDDEEFF00112233445566778E99|/"
    module = make_module(tmp_path, [_message(1950, body)], dry_run=False)
    result = asyncio.run(module._tick())
    item = parse_message(body, message_id=1950)
    assert item is not None and item.is_fc2 is True and item.code == "FC2-1234567"
    # 不入库、不抓元数据
    assert module.importer_calls == []
    assert result["fc2"] == 1
    entry = module._state["processed"]["1950"]
    assert entry["status"] == "fc2"
    assert entry["code"] == "FC2-1234567"
def test_fc2_import_switch_writes_row_without_metadata(tmp_path: Path) -> None:
    body = "FC2-PPV-7654321 素人\ned2k://|file|FC2-7654321_LTX.mp4|1234567|AABBCCDDEEFF001122334455667788FF|/"
    module = make_module(tmp_path, [_message(1951, body)], dry_run=False)
    module.config.fc2_import = True
    result = asyncio.run(module._tick())
    assert result["imported"] == 1
    row = module.importer_calls[0]["rows"][0]
    assert row["code"] == "FC2-7654321"
    assert row["cover_url"] == ""
    assert row["author"] == ""
def test_list_dialogs_returns_groups_from_user_session(tmp_path: Path) -> None:
    module = make_module(tmp_path, [], dry_run=False)

    class Entity:
        id = 123
        megagroup = True
        broadcast = False
        username = "grp"

    class Dialog:
        entity = Entity()
        is_group = True
        is_channel = False
        title = "群A"

    class BotEntity:
        id = 9
        megagroup = False
        broadcast = False
        username = "somebot"

    class BotDialog:
        entity = BotEntity()
        is_group = False
        is_channel = False
        title = "某个机器人"

    class FakeClient:
        async def get_dialogs(self):
            return [Dialog(), BotDialog()]

    async def fake_ensure():
        return FakeClient()

    module._ensure_client = fake_ensure
    groups = asyncio.run(module.list_dialogs())
    assert [g["title"] for g in groups] == ["群A"]
    assert groups[0]["id"] == "-100123"
    assert groups[0]["kind"] == "group"
def test_daily_cron_time_parsing() -> None:
    from telegram_bot.modules.ltx_watch import _daily_cron_time

    assert _daily_cron_time("0 21 * * *") == (21, 0)
    assert _daily_cron_time("30 8 * * *") == (8, 30)
    assert _daily_cron_time("*/5 * * * *") is None
    assert _daily_cron_time("0 21 * * 1") is None
    assert _daily_cron_time("") is None


def test_notify_fires_after_scheduled_time_and_once_per_day(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime as real_datetime

    from telegram_bot.modules import ltx_watch as lw

    module = make_module(tmp_path, [], dry_run=False)
    module.config.notify_enabled = True
    module.config.notify_cron = "0 21 * * *"
    sent: list[str] = []

    async def fake_send(reset: bool = True):
        sent.append("ok")
        module._state["last_notify_at"] = "2026-11-03 21:07:00"
        return {"ok": True}

    module._send_summary = fake_send

    class FakeDT(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 11, 3, 21, 7, 30)

    monkeypatch.setattr(lw, "datetime", FakeDT)
    asyncio.run(module._maybe_notify())
    assert sent == ["ok"]
    asyncio.run(module._maybe_notify())  # 同一天不重复
    assert sent == ["ok"]


def test_notify_waits_before_scheduled_time(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime as real_datetime

    from telegram_bot.modules import ltx_watch as lw

    module = make_module(tmp_path, [], dry_run=False)
    module.config.notify_enabled = True
    module.config.notify_cron = "0 21 * * *"
    sent: list[str] = []

    async def fake_send(reset: bool = True):
        sent.append("ok")
        return {"ok": True}

    module._send_summary = fake_send

    class FakeDT(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 11, 3, 20, 55, 0)

    monkeypatch.setattr(lw, "datetime", FakeDT)
    asyncio.run(module._maybe_notify())
    assert sent == []


def test_search_detail_uses_flaresolverr(monkeypatch) -> None:
    from telegram_bot.shared import javdb

    seen: list[str] = []

    def fake_fs(url, endpoint, **kwargs):
        seen.append((url, endpoint))
        if "/search?" in url:
            return '<a href="/v/AbCdE">x</a>'
        return ('<title>ABC-123 テスト</title>'
                '<img class="video-cover" src="https://c0.jdbstatic.com/covers/ab/abcd.jpg">'
                '<span class="symbol"><strong>ABC-123</strong></span>'
                '<span class="value"><a>某演员</a></span>')

    monkeypatch.setattr(javdb, "fetch_via_flaresolverr", fake_fs)
    data = javdb.search_detail_by_code("ABC-123", flaresolverr="http://192.0.2.10:8191")
    assert data is not None
    assert data["cover_url"].endswith("abcd.jpg")
    assert seen and seen[0][1] == "http://192.0.2.10:8191"
    assert any("javdb.com/v/AbCdE" in u for u, _ in seen)


def test_normalize_channel_accepts_username_id_and_link() -> None:
    from telegram_bot.modules.ltx_watch import normalize_channel

    assert normalize_channel("examplechan") == "examplechan"
    assert normalize_channel(" @examplechan ") == "examplechan"
    assert normalize_channel("https://t.me/examplechan") == "examplechan"
    assert normalize_channel("t.me/s/examplechan") == "examplechan"
    # 频道主取消用户名后只能按数字 ID 访问
    assert normalize_channel("-1001234567890") == -1001234567890
    assert normalize_channel(" -1001234567890 ") == -1001234567890
    assert normalize_channel("") == ""
    assert normalize_channel(None) == ""


def test_resolve_channel_explains_released_username(tmp_path: Path) -> None:
    module = make_module(tmp_path, [], dry_run=False, channel="examplechan")

    class FakeClient:
        async def get_entity(self, target):
            raise ValueError('No user has "examplechan" as username')

    try:
        asyncio.run(module._resolve_channel(FakeClient()))
    except RuntimeError as exc:
        assert "数字 ID" in str(exc)
    else:  # pragma: no cover - 防回归
        raise AssertionError("用户名失效时应给出可操作提示")


def test_resolve_channel_passes_numeric_id_to_get_entity(tmp_path: Path) -> None:
    module = make_module(tmp_path, [], dry_run=False, channel="-1001234567890")
    seen: list[object] = []

    class FakeClient:
        async def get_entity(self, target):
            seen.append(target)
            return SimpleNamespace(id=4419698318, title="Jasna LTX 频道")

    entity = asyncio.run(module._resolve_channel(FakeClient()))
    assert entity.id == 4419698318
    assert seen == [-1001234567890]

def test_parse_message_keeps_115_secupload_param() -> None:
    """带 115 秒传参数 h=<base32> 的链接要整段保留，且不能污染标题。"""
    body = (
        "🎬 女僕教育。-冇落貴族 瑠璃川椿- 真人版 渚光希 #LTX #ZEX-421 #自制\n"
        "ed2k://|file|ZEX-421-C_LTX.mp4|2862412725|4E0845D4A13EA9FC8C041D8C9755CBF3|"
        "h=M3LE3UQ7FLAEY4BRIRE4YTFUGJX5CNI|/\n"
        "#发布_20261107_225522"
    )
    item = parse_message(body, message_id=1)
    assert item is not None
    assert item.ed2k.endswith("|h=M3LE3UQ7FLAEY4BRIRE4YTFUGJX5CNI|/")
    assert item.magnet_hash == "4e0845d4a13ea9fc8c041d8c9755cbf3"
    assert "h=" not in item.title
    assert "ed2k" not in item.title
    assert item.title.startswith("女僕教育")
