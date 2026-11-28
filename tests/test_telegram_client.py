from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from telegram_bot.config import TelegramConfig
from telegram_bot.telegram.client import ProgrammaticMessageTracker, TelegramAdapter


def test_programmatic_tracker_covers_event_before_send_returns() -> None:
    tracker = ProgrammaticMessageTracker()
    token = tracker.begin("send_message", (-1001, "自动消息"), {})
    outgoing = SimpleNamespace(chat_id=-1001, id=21, raw_text="自动消息")

    assert tracker.is_programmatic(outgoing) is True

    tracker.finish(token, outgoing)

    assert tracker.is_programmatic(outgoing) is True


def test_pending_media_send_without_caption_waits_for_result_id() -> None:
    tracker = ProgrammaticMessageTracker()
    token = tracker.begin("send_file", (-1001, object()), {})

    manual_text = SimpleNamespace(chat_id=-1001, id=22, raw_text="本人消息", media=None)
    automatic_media = SimpleNamespace(chat_id=-1001, id=23, raw_text="", media=object())

    assert tracker.is_programmatic(manual_text) is False
    assert tracker.is_programmatic(automatic_media) is False

    tracker.finish(token, automatic_media)

    assert tracker.is_programmatic(automatic_media) is True


def test_pending_forward_does_not_overmatch_manual_forward() -> None:
    tracker = ProgrammaticMessageTracker()
    tracker.begin("forward_messages", (-1001, object()), {})
    manual_forward = SimpleNamespace(
        chat_id=-1001,
        id=24,
        raw_text="本人转发",
        fwd_from=object(),
    )

    assert tracker.is_programmatic(manual_forward) is False


def test_telegram_adapter_wraps_direct_client_sends_from_feature_modules(tmp_path: Path) -> None:
    class FakeTelegramClient:
        async def send_message(self, chat_id: int, text: str) -> object:
            return SimpleNamespace(chat_id=chat_id, id=31, raw_text=text)

    async def callback(event: object) -> None:
        return None

    adapter = TelegramAdapter(TelegramConfig(), tmp_path, callback)
    adapter.client = FakeTelegramClient()
    adapter._install_programmatic_message_tracking()

    sent = asyncio.run(adapter.client.send_message(-1001, "其他模块消息"))

    assert adapter.is_programmatic_message(sent) is True


def test_telegram_adapter_iterates_history_oldest_first_after_checkpoint(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakeTelegramClient:
        async def iter_messages(self, entity: int, **kwargs: object):
            captured["entity"] = entity
            captured["kwargs"] = kwargs
            for message_id in (11, 12):
                yield SimpleNamespace(id=message_id)

    async def callback(event: object) -> None:
        return None

    async def collect() -> list[int]:
        adapter = TelegramAdapter(TelegramConfig(), tmp_path, callback)
        adapter.client = FakeTelegramClient()
        adapter.connected = True
        return [
            message.id
            async for message in adapter.iter_history_messages("-100123", min_id=10)
        ]

    assert asyncio.run(collect()) == [11, 12]
    assert captured["entity"] == -100123
    assert captured["kwargs"] == {"min_id": 10, "reverse": True}


def test_empty_monitor_selection_filters_messages_before_dispatch(tmp_path: Path) -> None:
    config = TelegramConfig()
    callback = AsyncMock()
    adapter = TelegramAdapter(config, tmp_path, callback)
    message = SimpleNamespace(chat_id=-100123, raw_text="test message")
    private_message = SimpleNamespace(chat_id=123, raw_text="test message")

    async def receive() -> None:
        await adapter._handle_monitored_event(message, "message")
        callback.assert_not_called()
        dialog = SimpleNamespace(id=-100123, entity=SimpleNamespace(id=100123), is_group=True, title="Group")
        adapter.client = SimpleNamespace(get_dialogs=AsyncMock(return_value=[dialog]))
        adapter.connected = True
        groups = await adapter.list_groups()
        adapter.update_monitored_groups([groups[0]["id"]])
        await adapter._handle_monitored_event(private_message, "message")
        callback.assert_not_called()
        await adapter._handle_monitored_event(message, "message")
        callback.assert_awaited_once_with(message)
        config.monitor_all = True
        await adapter._handle_monitored_event(private_message, "message")
        assert callback.await_count == 2

    asyncio.run(receive())


def test_bot_reconnect_uses_account_scoped_sessions_and_checks_identity(tmp_path: Path, monkeypatch) -> None:
    import telethon

    clients = []

    class FakeTelegramClient:
        def __init__(self, session, api_id, api_hash, **kwargs):
            self.session = Path(session)
            self.closed = False
            clients.append(self)

        async def start(self, *, bot_token):
            self.bot_id = int(bot_token.split(":")[0])

        async def get_me(self):
            actual_id = self.bot_id if self.bot_id != 33333 else 22222
            return SimpleNamespace(id=actual_id, bot=True, username=f"bot{actual_id}")

        def on(self, event):
            return lambda handler: handler

        async def disconnect(self):
            self.closed = True

    monkeypatch.setattr(telethon, "TelegramClient", FakeTelegramClient)
    config = TelegramConfig(enabled=True, api_id=1, api_hash="test", bot_token="11111:test")
    adapter = TelegramAdapter(config, tmp_path, AsyncMock())

    async def connect_and_switch() -> None:
        await adapter.start()
        assert adapter.connected and adapter.username == "bot11111"
        config.bot_token = "22222:test"
        await adapter.reconnect()
        assert clients[0].closed
        assert adapter.connected and adapter.username == "bot22222"
        assert [client.session.name for client in clients] == [
            "telegram_bot.bot-11111.session", "telegram_bot.bot-22222.session",
        ]
        config.bot_token = "33333:test"
        await adapter.reconnect()
        assert clients[-1].closed
        assert not adapter.connected and adapter.client is None
        assert "身份不一致" in adapter.last_error

    asyncio.run(connect_and_switch())
