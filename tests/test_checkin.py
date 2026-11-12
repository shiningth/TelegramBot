from __future__ import annotations

# ruff: noqa: E402

import asyncio
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from telegram_bot.config import AppConfig, CheckinConfig, CheckinTaskConfig
from telegram_bot.modules.base import ModuleCard
from telegram_bot.modules.checkin import (
    CheckinModule,
    cron_matches,
    describe_cron_schedule,
    due_checkin_cron,
    validate_cron_schedule,
)


def _card() -> ModuleCard:
    return ModuleCard(
        id="checkin",
        name="自动签到",
        description="测试",
        category="automation",
        enabled=True,
    )


def test_cron_matches_minute_interval_daily_and_weekly_schedules() -> None:
    monday = datetime(2026, 8, 24, 9, 0)

    assert cron_matches("*/30 * * * *", monday.replace(minute=30)) is True
    assert cron_matches("*/30 * * * *", monday.replace(minute=29)) is False
    assert cron_matches("0 9 * * *", monday) is True
    assert cron_matches("0 9 * * *", monday.replace(hour=10)) is False
    assert cron_matches("0 9 * * 1", monday) is True
    assert cron_matches("0 9 * * 1", monday.replace(day=25)) is False


def test_cron_supports_day_intervals_ranges_and_lists() -> None:
    assert cron_matches("0 9 */3 * *", datetime(2026, 8, 1, 9, 0)) is True
    assert cron_matches("0 9 */3 * *", datetime(2026, 8, 4, 9, 0)) is True
    assert cron_matches("0 9 */3 * *", datetime(2026, 8, 2, 9, 0)) is False
    assert cron_matches("15 8-10 * * 1,3,5", datetime(2026, 8, 26, 10, 15)) is True
    assert cron_matches("15 8-10 * * 1,3,5", datetime(2026, 8, 27, 10, 15)) is False


def test_due_checkin_cron_accepts_multiple_expressions_and_returns_minute_key() -> None:
    schedule = "0 9 * * 1\n30 18 * * 5"

    assert due_checkin_cron(schedule, datetime(2026, 8, 24, 9, 0, 42)) == "09:00:00"
    assert due_checkin_cron(schedule, datetime(2026, 8, 28, 18, 30, 1)) == "18:30:00"
    assert due_checkin_cron(schedule, datetime(2026, 8, 25, 9, 0)) is None


def test_cron_descriptions_explain_common_notification_forms() -> None:
    assert describe_cron_schedule("*/30 * * * *") == "每隔 30 分钟发送一次"
    assert describe_cron_schedule("0 9 * * *") == "每天 09:00 发送"
    assert (
        describe_cron_schedule("0 9 */3 * *")
        == "每月从 1 日开始每隔 3 天，于 09:00 发送"
    )
    assert describe_cron_schedule("0 9 * * 1") == "每周一 09:00 发送"
    assert describe_cron_schedule("0 9 * * 1,3,5") == "每周一、周三、周五 09:00 发送"


def test_invalid_cron_is_rejected_with_actionable_error() -> None:
    invalid_values = (
        "",
        "0 9 * *",
        "60 9 * * *",
        "*/0 * * * *",
        "0 9 * * 8",
    )

    for value in invalid_values:
        try:
            validate_cron_schedule(value)
        except ValueError as exc:
            assert "Cron" in str(exc)
        else:
            raise AssertionError(f"invalid cron was accepted: {value!r}")


def test_legacy_daily_times_are_exposed_as_equivalent_cron(tmp_path: Path) -> None:
    task = CheckinTaskConfig(
        id="legacy", times=["09:00", "18:30"], message="签到", target="@bot"
    )
    module = CheckinModule(_card(), CheckinConfig(tasks=[task]), tmp_path)

    snapshot = module.card.config["tasks"][0]
    assert snapshot["cron"] == "0 9 * * *\n30 18 * * *"


def test_checkin_tick_uses_cron_and_deduplicates_within_the_minute(
    tmp_path: Path,
) -> None:
    sent: list[tuple[str, str]] = []

    async def sender(target: str, message: str) -> None:
        sent.append((target, message))

    task = CheckinTaskConfig(
        id="half-hour",
        name="半小时提醒",
        target="@bot",
        cron="*/30 * * * *",
        message="签到",
    )
    module = CheckinModule(_card(), CheckinConfig(tasks=[task]), tmp_path, sender)
    due = datetime(2026, 8, 24, 12, 30, 5)

    asyncio.run(module._tick(due))
    asyncio.run(module._tick(due.replace(second=45)))
    asyncio.run(module._tick(due.replace(minute=31)))

    assert sent == [("@bot", "签到")]


def test_failed_checkin_retries_after_cron_minute_passes(tmp_path: Path) -> None:
    """04:00 发送失败（网络抖动）时，之后几分钟要能补发成功。"""
    attempts: list[tuple[str, str]] = []
    state = {"fail": True}

    async def sender(target: str, message: str) -> None:
        attempts.append((target, message))
        if state["fail"]:
            raise ConnectionResetError("Connection reset by peer")

    task = CheckinTaskConfig(
        id="daily",
        name="每日签到",
        target="@bot",
        cron="0 4 * * *",
        message="/checkin",
    )
    module = CheckinModule(_card(), CheckinConfig(tasks=[task]), tmp_path, sender)

    asyncio.run(module._tick(datetime(2026, 11, 12, 4, 0, 5)))
    assert module.sent_keys == {}

    state["fail"] = False
    asyncio.run(module._tick(datetime(2026, 11, 12, 4, 6, 0)))

    assert len(attempts) == 2
    assert module.sent_keys == {"2026-11-12|daily|04:00:00": module.sent_keys["2026-11-12|daily|04:00:00"]}
    asyncio.run(module._tick(datetime(2026, 11, 12, 4, 7, 0)))
    assert len(attempts) == 2


def test_failed_checkin_respects_retry_interval_and_gives_up(tmp_path: Path) -> None:
    calls: list[int] = []

    async def sender(target: str, message: str) -> None:
        calls.append(1)
        raise TimeoutError("flood wait")

    task = CheckinTaskConfig(
        id="daily",
        name="每日签到",
        target="@bot",
        cron="0 4 * * *",
        message="/checkin",
    )
    config = CheckinConfig(tasks=[task], retry_interval_seconds=300, max_attempts=2)
    module = CheckinModule(_card(), config, tmp_path, sender)

    asyncio.run(module._tick(datetime(2026, 11, 12, 4, 0, 5)))
    asyncio.run(module._tick(datetime(2026, 11, 12, 4, 1, 5)))  # 未到重试间隔
    assert len(calls) == 1

    asyncio.run(module._tick(datetime(2026, 11, 12, 4, 6, 0)))
    assert len(calls) == 2  # 超过 300 秒后再试一次

    asyncio.run(module._tick(datetime(2026, 11, 12, 4, 12, 0)))
    assert len(calls) == 2  # 达到 max_attempts 后当天不再打


def test_one_failing_task_does_not_block_the_others(tmp_path: Path) -> None:
    sent: list[str] = []

    async def sender(target: str, message: str) -> None:
        if target == "@broken":
            raise RuntimeError("boom")
        sent.append(target)

    tasks = [
        CheckinTaskConfig(id="a", name="A", target="@broken", cron="0 4 * * *", message="x"),
        CheckinTaskConfig(id="b", name="B", target="@ok", cron="0 4 * * *", message="y"),
    ]
    module = CheckinModule(_card(), CheckinConfig(tasks=tasks), tmp_path, sender)

    asyncio.run(module._tick(datetime(2026, 11, 12, 4, 0, 5)))

    assert sent == ["@ok"]


def test_invalid_admin_update_keeps_previous_checkin_tasks(tmp_path: Path) -> None:
    original = CheckinTaskConfig(
        id="daily",
        target="@bot",
        cron="0 9 * * *",
        message="签到",
    )
    module = CheckinModule(_card(), CheckinConfig(tasks=[original]), tmp_path)

    try:
        module.update_config(
            {
                "tasks": [
                    {
                        "id": "broken",
                        "target": "@bot",
                        "cron": "99 9 * * *",
                        "message": "签到",
                    }
                ]
            }
        )
    except ValueError as exc:
        assert "分钟字段" in str(exc)
    else:
        raise AssertionError("invalid admin update was accepted")

    assert module.config.tasks == [original]
    assert module.card.config["tasks"][0]["cron"] == "0 9 * * *"


def test_invalid_poll_interval_does_not_partially_apply_new_tasks(
    tmp_path: Path,
) -> None:
    original = CheckinTaskConfig(
        id="daily",
        target="@bot",
        cron="0 9 * * *",
        message="签到",
    )
    config = CheckinConfig(tasks=[original], poll_interval_seconds=10.0)
    module = CheckinModule(_card(), config, tmp_path)

    try:
        module.update_config(
            {
                "tasks": [
                    {
                        "id": "weekly",
                        "target": "@bot",
                        "cron": "0 9 * * 1",
                        "message": "签到",
                    }
                ],
                "poll_interval_seconds": "invalid",
            }
        )
    except ValueError:
        pass
    else:
        raise AssertionError("invalid poll interval was accepted")

    assert module.config.tasks == [original]
    assert module.config.poll_interval_seconds == 10.0
    assert module.card.config["tasks"][0]["id"] == "daily"


def test_checkin_config_roundtrip_preserves_cron(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = AppConfig.from_dict(
        {
            "checkin": {
                "tasks": [
                    {
                        "id": "weekly",
                        "name": "周签到",
                        "enabled": True,
                        "target": "@bot",
                        "cron": "0 9 * * 1",
                        "message": "签到",
                    }
                ]
            }
        }
    )
    config.save(path)

    loaded = AppConfig.load(path)

    assert loaded.checkin.tasks[0].cron == "0 9 * * 1"


def test_checkin_admin_uses_cron_input_examples_and_live_description() -> None:
    static_dir = ROOT / "telegram_bot" / "admin" / "static"
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")

    assert 'data-field="cron"' in app_js
    assert 'data-field="times"' not in app_js
    assert "function describeCronSchedule" in app_js
    assert "function updateCronSummary" in app_js
    assert 'class="cron-summary"' in app_js
    assert 'data-cron="*/30 * * * *"' in app_js
    assert "分 时 日 月 周" in index_html


def test_retry_delay_grows_but_caps(tmp_path: Path) -> None:
    module = CheckinModule(
        _card(), CheckinConfig(tasks=[], retry_interval_seconds=300), tmp_path, None
    )

    assert module._retry_delay(0) == 300
    assert module._retry_delay(1) == 300
    assert module._retry_delay(4) == 600
    assert module._retry_delay(10) == 1200
    assert module._retry_delay(19) == 1800


def test_checkin_notifies_failure_and_recovery(tmp_path: Path, monkeypatch) -> None:
    import telegram_bot.modules.checkin as checkin_module

    sent: list[str] = []

    async def fake_send(token: str, chat_id: str, text: str, *, timeout: float = 20.0) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(checkin_module, "send_bot_message_async", fake_send)

    state = {"fail": True}

    async def sender(target: str, message: str) -> None:
        if state["fail"]:
            raise ConnectionResetError("Connection reset by peer")

    task = CheckinTaskConfig(
        id="daily",
        name="每日签到",
        target="@bot",
        cron="0 4 * * *",
        message="/checkin",
    )
    config = CheckinConfig(
        tasks=[task],
        retry_interval_seconds=60,
        notify_bot_token="token",
        notify_chat_id="123456789",
    )
    module = CheckinModule(_card(), config, tmp_path, sender)

    asyncio.run(module._tick(datetime(2026, 11, 12, 4, 0, 5)))
    assert len(sent) == 1
    assert "签到失败" in sent[0] and "Connection reset" in sent[0]

    state["fail"] = False
    asyncio.run(module._tick(datetime(2026, 11, 12, 4, 1, 30)))
    assert len(sent) == 2
    assert "补发成功" in sent[1]

    asyncio.run(module._tick(datetime(2026, 11, 12, 4, 2, 0)))
    assert len(sent) == 2  # 成功后当天不再重复


def test_checkin_notifies_give_up_once(tmp_path: Path, monkeypatch) -> None:
    import telegram_bot.modules.checkin as checkin_module

    sent: list[str] = []

    async def fake_send(token: str, chat_id: str, text: str, *, timeout: float = 20.0) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(checkin_module, "send_bot_message_async", fake_send)

    async def sender(target: str, message: str) -> None:
        raise TimeoutError("flood wait")

    task = CheckinTaskConfig(id="daily", name="每日签到", target="@bot", cron="0 4 * * *", message="/x")
    config = CheckinConfig(
        tasks=[task],
        retry_interval_seconds=60,
        max_attempts=2,
        notify_bot_token="token",
        notify_chat_id="1",
    )
    module = CheckinModule(_card(), config, tmp_path, sender)

    asyncio.run(module._tick(datetime(2026, 11, 12, 4, 0, 5)))
    asyncio.run(module._tick(datetime(2026, 11, 12, 4, 2, 0)))
    asyncio.run(module._tick(datetime(2026, 11, 12, 4, 9, 0)))

    assert len(sent) == 2
    assert "签到失败" in sent[0]
    assert "签到放弃" in sent[1]


def test_blank_notify_token_keeps_saved_value(tmp_path: Path) -> None:
    module = CheckinModule(
        _card(), CheckinConfig(tasks=[], notify_bot_token="saved-token"), tmp_path, None
    )

    module.update_config(
        {
            "notify_bot_token": "",
            "notify_chat_id": "42",
            "notify_enabled": False,
            "catchup_minutes": 600,
        }
    )

    assert module.config.notify_bot_token == "saved-token"
    assert module.config.notify_chat_id == "42"
    assert module.config.notify_enabled is False
    assert module.config.catchup_minutes == 600


def test_repeated_failures_only_notify_once(tmp_path: Path, monkeypatch) -> None:
    import telegram_bot.modules.checkin as checkin_module

    sent: list[str] = []

    async def fake_send(token: str, chat_id: str, text: str, *, timeout: float = 20.0) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(checkin_module, "send_bot_message_async", fake_send)

    async def sender(target: str, message: str) -> None:
        raise TimeoutError("still down")

    task = CheckinTaskConfig(id="daily", name="每日签到", target="@bot", cron="0 4 * * *", message="/x")
    config = CheckinConfig(
        tasks=[task],
        retry_interval_seconds=60,
        notify_bot_token="token",
        notify_chat_id="1",
    )
    module = CheckinModule(_card(), config, tmp_path, sender)

    for minute in (0, 2, 5, 9):
        asyncio.run(module._tick(datetime(2026, 11, 12, 4, minute, 5)))

    assert len(sent) == 1
    assert "签到失败" in sent[0]


