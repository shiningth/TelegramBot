from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


class PersonalCheckinSender:
    """Use an existing user session for check-in without changing the group bot."""

    def __init__(self, telegram: Any, checkin: Any, root: Path, fallback: Any, *, factory: Any = None):
        self.telegram = telegram
        self.checkin = checkin
        self.root = root
        self.fallback = fallback
        self.factory = factory
        self.client: Any = None
        self._identity: tuple[str, str] | None = None
        self._lock = asyncio.Lock()

    async def __call__(self, target: str, text: str) -> None:
        if not self.checkin.user_session_name:
            await self.fallback(target, text)
            return
        async with self._lock:
            await self._connect()
            await self.client.send_message(target, text, parse_mode=None)

    async def _connect(self) -> None:
        name = self.checkin.user_session_name
        user_id = self.checkin.user_id
        if not user_id.isdigit():
            raise ValueError("Personal check-in requires the expected numeric user ID")
        if Path(name).name != name or name in {".", "..", self.telegram.session_name}:
            raise ValueError("Check-in requires a separate user session name")
        directory = Path(self.telegram.session_dir)
        if not directory.is_absolute():
            directory = self.root / directory
        session = directory / name
        if not Path(str(session) + ".session").is_file():
            raise RuntimeError("Personal check-in session does not exist; log in first")
        identity = (str(session), user_id)
        if self.client is not None and self._identity != identity:
            await self.stop()
        if self.client is None:
            factory = self.factory
            if factory is None:
                from telethon import TelegramClient
                factory = TelegramClient
            self.client = factory(str(session), self.telegram.api_id, self.telegram.api_hash)
        try:
            if not self.client.is_connected():
                await self.client.connect()
            if not await self.client.is_user_authorized():
                raise RuntimeError("Personal check-in session needs login")
            me = await self.client.get_me()
            if me.bot or str(me.id) != user_id:
                raise RuntimeError("Personal check-in session belongs to a different account")
            self._identity = identity
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        client, self.client = self.client, None
        self._identity = None
        if client is not None:
            await client.disconnect()
