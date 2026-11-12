import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from telegram_bot.config import CheckinConfig, TelegramConfig
from telegram_bot.shared.checkin_sender import PersonalCheckinSender


def make_sender(tmp_path, *, user_id=123456789, bot=False, authorized=True):
    (tmp_path / 'user_session.session').touch()
    client = SimpleNamespace(is_connected=lambda: False, connect=AsyncMock(),
        disconnect=AsyncMock(), is_user_authorized=AsyncMock(return_value=authorized),
        get_me=AsyncMock(return_value=SimpleNamespace(id=user_id, bot=bot)), send_message=AsyncMock())
    fallback = AsyncMock()
    sender = PersonalCheckinSender(TelegramConfig(session_dir=str(tmp_path)),
        CheckinConfig(user_session_name='user_session', user_id='123456789'),
        tmp_path, fallback, factory=lambda *args: client)
    return sender, client, fallback


def test_personal_session_sends_as_expected_user(tmp_path):
    sender, client, fallback = make_sender(tmp_path)
    async def run():
        await sender('@example_checkin_bot', '/checkin')
        await sender.stop()
    asyncio.run(run())
    client.send_message.assert_awaited_once_with('@example_checkin_bot', '/checkin', parse_mode=None)
    client.disconnect.assert_awaited_once()
    fallback.assert_not_awaited()


@pytest.mark.parametrize('kwargs', [{'user_id': 1}, {'bot': True}, {'authorized': False}])
def test_wrong_or_logged_out_account_cannot_send(tmp_path, kwargs):
    sender, client, fallback = make_sender(tmp_path, **kwargs)
    with pytest.raises(RuntimeError):
        asyncio.run(sender('@example_checkin_bot', '/checkin'))
    client.send_message.assert_not_awaited()
    fallback.assert_not_awaited()
    client.disconnect.assert_awaited_once()


def test_missing_session_does_not_create_or_login(tmp_path):
    sender, client, _ = make_sender(tmp_path)
    (tmp_path / 'user_session.session').unlink()
    with pytest.raises(RuntimeError, match='does not exist'):
        asyncio.run(sender('@target', '/checkin'))
    client.connect.assert_not_awaited()


def test_default_preserves_existing_sender(tmp_path):
    sender, client, fallback = make_sender(tmp_path)
    sender.checkin.user_session_name = ''
    asyncio.run(sender('@target', '/checkin'))
    fallback.assert_awaited_once_with('@target', '/checkin')
    client.connect.assert_not_awaited()


def test_group_bot_session_cannot_be_used_for_personal_checkin(tmp_path):
    sender, client, _ = make_sender(tmp_path)
    sender.checkin.user_session_name = sender.telegram.session_name
    with pytest.raises(ValueError, match='separate'):
        asyncio.run(sender('@target', '/checkin'))
    client.connect.assert_not_awaited()
