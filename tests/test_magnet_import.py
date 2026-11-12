from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from telegram_bot.config import AppConfig, MagnetImportConfig, magnet_import_config_from_dict
from telegram_bot.modules.base import ModuleCard
from telegram_bot.modules.magnet_import import MagnetImportModule
from telegram_bot.shared.telegram_download import download_document_resumable

CHAT_ID = -1000000000002
CONTENT = b"id,title\n" + b"x" * 3000


class FakeClient:
    def __init__(self, content: bytes = CONTENT, fail_after: int | None = None) -> None:
        self.content = content
        self.fail_after = fail_after
        self.offsets: list[int] = []
        self.sent: list[dict[str, object]] = []
        self.refetched = 0

    async def iter_download(self, media: object, *, offset: int = 0, request_size: int = 1024, file_size: int | None = None):
        self.offsets.append(offset)
        position = offset
        while position < len(self.content):
            if self.fail_after is not None and position >= self.fail_after:
                self.fail_after = None
                raise ConnectionError("network dropped")
            yield self.content[position : position + request_size]
            position += request_size

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})

    async def get_messages(self, chat_id: int, ids: int) -> object:
        self.refetched += 1
        return None


class FakeImporter:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.error = error

    def __call__(self, base_url, username, password, file_path, file_name, **kwargs):
        self.calls.append(
            {"base_url": base_url, "username": username, "password": password, "name": file_name,
             "content": Path(file_path).read_bytes(), **kwargs}
        )
        if self.error:
            raise self.error
        return {
            "success": True,
            "data": {"imported_records": 10, "updated_records": 1, "skipped_records": 2},
        }


def make_message(*, name="x1080x_all_2026-10-09.csv", username="example_sender", chat_id=CHAT_ID, message_id=7, size=None):
    return SimpleNamespace(
        id=message_id,
        chat_id=chat_id,
        sender_id=123,
        sender=SimpleNamespace(username=username),
        document=SimpleNamespace(size=size if size is not None else len(CONTENT)),
        file=SimpleNamespace(name=name, size=size if size is not None else len(CONTENT)),
    )


def make_module(tmp_path: Path, importer=None, **overrides) -> MagnetImportModule:
    overrides.setdefault("watch_chats", [str(CHAT_ID)])
    overrides.setdefault("sender_usernames", ["example_sender"])
    config = MagnetImportConfig(api_password="secret", **overrides)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    module = MagnetImportModule(
        ModuleCard("magnet_import", "magnet", "", "automation", enabled=True),
        config,
        tmp_path,
        importer=importer or FakeImporter(),
        sleep=fake_sleep,
    )
    module.sleeps = sleeps
    return module


async def _dispatch(module: MagnetImportModule, message, client):
    result = await module.handle_message(SimpleNamespace(message=message, chat_id=message.chat_id, client=client))
    await module.wait_idle()
    return result


def test_config_defaults_and_parsing() -> None:
    config = AppConfig.from_dict(AppConfig.defaults_dict())
    assert config.magnet_import.watch_chats == []
    assert config.magnet_import.sender_usernames == []
    assert config.modules.magnet_import is False
    assert config.magnet_import.manual_only is False
    assert magnet_import_config_from_dict({"manual_only": True}).manual_only is True
    assert config.magnet_import.file_name_pattern == r"^x1080x.*\.csv$"

    parsed = magnet_import_config_from_dict({"sender_usernames": ["@Foo"], "download_retries": 99, "api_base_url": "http://a/"})
    assert parsed.sender_usernames == ["Foo"]
    assert parsed.download_retries == 20
    assert parsed.api_base_url == "http://a"


def test_password_is_masked_and_preserved(tmp_path: Path) -> None:
    module = make_module(tmp_path)
    assert module.card.config["api_password"] == ""
    assert module.card.config["has_api_password"] is True

    module.update_config({"api_password": "", "reply_text": "ok"})
    assert module.config.api_password == "secret"
    assert module.config.reply_text == "ok"
    module.update_config({"api_password": "new"})
    assert module.config.api_password == "new"

    app = AppConfig()
    app.magnet_import.api_password = "secret"
    assert app.public_dict()["magnet_import"]["api_password"] != "secret"


def test_imports_csv_replies_and_cleans_up(tmp_path: Path) -> None:
    importer = FakeImporter()
    module = make_module(tmp_path, importer)
    client = FakeClient()

    result = asyncio.run(_dispatch(module, make_message(), client))

    assert result.status == "handled"
    assert len(importer.calls) == 1
    call = importer.calls[0]
    assert call["content"] == CONTENT
    assert call["password"] == "secret"
    assert call["update_existing"] is False and call["skip_duplicates"] is True
    assert client.sent == [{"chat_id": CHAT_ID, "text": "已自动导入，谢谢老大", "reply_to": 7}]
    assert list((tmp_path / "runtime/tmp/magnet_import").iterdir()) == []
    state = json.loads((tmp_path / "runtime/data/magnet_import_state.json").read_text(encoding="utf-8"))
    assert state["processed"][f"{CHAT_ID}:7"]["status"] == "imported"
    assert module.card.metrics["imported"] == 1

    # 同一消息（编辑或重复事件）不会再次导入
    asyncio.run(_dispatch(module, make_message(), client))
    assert len(importer.calls) == 1


def test_state_survives_restart(tmp_path: Path) -> None:
    asyncio.run(_dispatch(make_module(tmp_path), make_message(), FakeClient()))
    importer = FakeImporter()
    module = make_module(tmp_path, importer)
    result = asyncio.run(_dispatch(module, make_message(), FakeClient()))
    assert result.status == "ignored"
    assert importer.calls == []


def test_ignores_unmatched_messages(tmp_path: Path) -> None:
    importer = FakeImporter()
    module = make_module(tmp_path, importer)
    client = FakeClient()
    for message in (
        make_message(username="someone"),
        make_message(chat_id=-100999),
        make_message(name="notes.txt"),
        make_message(name="other_all_2026-10-09.csv"),
        make_message(name="backup_x1080x_2026-10-09.csv"),
        make_message(name="x1080x_all_2026-10-09.txt"),
    ):
        assert asyncio.run(_dispatch(module, message, client)).status == "ignored"
    no_doc = make_message()
    no_doc.document = None
    assert asyncio.run(_dispatch(module, no_doc, client)).status == "ignored"
    assert importer.calls == []


def test_import_failure_does_not_reply_and_cleans_up(tmp_path: Path) -> None:
    importer = FakeImporter(RuntimeError("登录失败"))
    module = make_module(tmp_path, importer)
    client = FakeClient()

    asyncio.run(_dispatch(module, make_message(), client))

    assert client.sent == []
    assert list((tmp_path / "runtime/tmp/magnet_import").iterdir()) == []
    assert module.card.metrics["last"]["status"] == "failed"
    assert "登录失败" in module.card.metrics["last"]["detail"]


def test_missing_credentials_skip_download(tmp_path: Path) -> None:
    importer = FakeImporter()
    module = make_module(tmp_path, importer)
    module.config.api_password = ""
    client = FakeClient()
    asyncio.run(_dispatch(module, make_message(), client))
    assert client.offsets == [] and importer.calls == []


def test_download_resumes_after_failure(tmp_path: Path) -> None:
    client = FakeClient(fail_after=2048)
    message = make_message()
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def refetch():
        return await client.get_messages(CHAT_ID, ids=7)

    dest = tmp_path / "out.csv"
    size = asyncio.run(
        download_document_resumable(client, message, dest, retries=3, refetch=refetch, sleep=fake_sleep, request_size=1024)
    )
    assert size == len(CONTENT)
    assert dest.read_bytes() == CONTENT
    assert client.offsets == [0, 2048]
    assert client.refetched == 1 and len(sleeps) == 1
    assert not (tmp_path / "out.csv.part").exists()


def test_download_rejects_size_mismatch(tmp_path: Path) -> None:
    async def noop(_):
        return None

    client = FakeClient()
    message = make_message(size=len(CONTENT) + 10)
    try:
        asyncio.run(download_document_resumable(client, message, tmp_path / "x.csv", retries=2, sleep=noop, request_size=1024))
    except IOError as exc:
        assert "下载不完整" in str(exc)
    else:
        raise AssertionError("size mismatch should fail")


class ManualClient(FakeClient):
    def __init__(self, *, admin=False):
        super().__init__()
        self.admin = admin

    async def get_me(self):
        return SimpleNamespace(username="test_import_bot")

    async def get_permissions(self, chat_id, sender_id):
        return SimpleNamespace(is_admin=self.admin, is_creator=False)


def make_command(reply=None, *, text="@test_import_bot 导入", sender_id=123):
    async def get_reply_message():
        return reply
    return SimpleNamespace(id=90, chat_id=CHAT_ID, sender_id=sender_id,
                           sender=SimpleNamespace(username="example_sender"),
                           raw_text=text, get_reply_message=get_reply_message)


def test_manual_mode_does_not_import_upload(tmp_path):
    importer = FakeImporter()
    module = make_module(tmp_path, importer, manual_only=True)
    asyncio.run(_dispatch(module, make_message(), ManualClient()))
    assert importer.calls == []


def test_short_import_command_preserves_owner_authorization(tmp_path):
    for index, command in enumerate(("/import", "/导入")):
        _check_short_import_authorization(tmp_path / str(index), command)


def _check_short_import_authorization(tmp_path, command):
    importer = FakeImporter()
    module = make_module(tmp_path, importer, manual_only=True,
                         sender_ids=["123"], sender_usernames=[])
    client = ManualClient(admin=True)
    denied = make_command(make_message(), text=command, sender_id=999)
    asyncio.run(_dispatch(module, denied, client))
    assert importer.calls == []
    cmd = make_command(make_message(), text=command)
    asyncio.run(_dispatch(module, cmd, client))
    assert len(importer.calls) == 1
    assert client.sent[-1]["reply_to"] == cmd.id


def test_manual_reply_imports_other_senders_csv_and_deduplicates(tmp_path):
    importer = FakeImporter()
    module = make_module(tmp_path, importer, manual_only=True)
    client = ManualClient()
    cmd = make_command(make_message(username="other_uploader"))
    asyncio.run(_dispatch(module, cmd, client))
    asyncio.run(_dispatch(module, cmd, client))
    assert len(importer.calls) == 1
    assert client.sent[0]["reply_to"] == 90
    assert "处理" in client.sent[1]["text"]


def test_manual_ignores_other_bot_and_plain_mentions(tmp_path):
    importer = FakeImporter()
    module = make_module(tmp_path, importer, manual_only=True)
    for text in ["@other_bot 导入", "导入", "@test_import_bot", "@test_import_bot 导入 extra"]:
        asyncio.run(_dispatch(module, make_command(make_message(), text=text), ManualClient()))
    assert importer.calls == []


def test_manual_missing_csv_explains_usage(tmp_path):
    module = make_module(tmp_path, manual_only=True)
    client = ManualClient()
    asyncio.run(_dispatch(module, make_command(), client))
    asyncio.run(_dispatch(module, make_command(make_message(name="no.txt")), client))
    assert len(client.sent) == 2
    assert all("CSV" in x["text"] for x in client.sent)


def test_manual_without_allowlist_requires_group_admin(tmp_path):
    for admin in [False, True]:
        importer = FakeImporter()
        module = make_module(tmp_path / str(admin), importer, manual_only=True,
                             sender_usernames=[], sender_ids=[], silent_denied=False)
        client = ManualClient(admin=admin)
        asyncio.run(_dispatch(module, make_command(make_message()), client))
        assert len(importer.calls) == int(admin)
        if not admin:
            assert "管理员" in client.sent[0]["text"]


def test_unauthorized_command_is_silent_by_default(tmp_path):
    """别人发指令或 @机器人：默认一个字都不回，只在后台记拦截次数。"""
    importer = FakeImporter()
    module = make_module(
        tmp_path,
        importer,
        manual_only=True,
        manual_commands=["/import", "/导入", "导入"],
        sender_ids=["123"],
        sender_usernames=[],
    )
    client = ManualClient(admin=True)  # 连群管理员也不放行，因为授权名单非空
    for text in ("导入", "/导入", "/import", "@test_import_bot 导入"):
        result = asyncio.run(_dispatch(module, make_command(make_message(), text=text, sender_id=999), client))
        assert result.status == "ignored", text
    assert client.sent == []
    assert importer.calls == []
    assert module.card.metrics["denied"] == 4
    state = json.loads((tmp_path / "runtime/data/magnet_import_state.json").read_text(encoding="utf-8"))
    assert state["denied"] == 4


def test_manual_rejects_cross_chat_reply(tmp_path):
    importer = FakeImporter()
    module = make_module(tmp_path, importer, manual_only=True)
    asyncio.run(_dispatch(module, make_command(make_message(chat_id=-1009)), ManualClient()))
    assert importer.calls == []


def test_manual_failed_import_replies_and_allows_retry(tmp_path):
    importer = FakeImporter(RuntimeError("bad credentials"))
    module = make_module(tmp_path, importer, manual_only=True)
    client = ManualClient()
    cmd = make_command(make_message())
    asyncio.run(_dispatch(module, cmd, client))
    assert "导入失败" in client.sent[0]["text"]
    assert "bad credentials" not in client.sent[0]["text"]
    importer.error = None
    asyncio.run(_dispatch(module, cmd, client))
    assert len(importer.calls) == 2


def test_render_reply_only_replaces_known_placeholders():
    from telegram_bot.modules.magnet_import import render_reply

    assert render_reply("新增 {imported} 条，{unknown} 原样", imported=3) == "新增 3 条，{unknown} 原样"
    assert render_reply("花括号 { 与 } 不报错") == "花括号 { 与 } 不报错"
    assert render_reply("") == ""
    assert render_reply(None) == ""


def test_config_parses_commands_and_reply_texts():
    parsed = magnet_import_config_from_dict({})
    assert parsed.manual_commands == ["/import", "/导入"]
    assert parsed.manual_mention_commands == ["导入", "/import"]
    assert "{imported}" in parsed.manual_success_text
    assert parsed.silent_denied is True
    assert magnet_import_config_from_dict({"silent_denied": False}).silent_denied is False

    custom = magnet_import_config_from_dict(
        {
            "manual_commands": ["入库", " 入库 ", "", "/in"],
            "manual_mention_commands": [],
            "reply_denied_text": "",
            "reply_failed_text": "失败了：{file}",
        }
    )
    assert custom.manual_commands == ["入库", "/in"]
    # 空列表/空文案回落默认，避免后台误清空后无法触发或不回复
    assert custom.manual_mention_commands == ["导入", "/import"]
    assert custom.reply_denied_text == MagnetImportConfig().reply_denied_text
    assert custom.reply_failed_text == "失败了：{file}"


def test_manual_command_variants_are_accepted(tmp_path):
    for index, text in enumerate(
        ["/import", "/IMPORT", "/import@test_import_bot", "@test_import_bot 导入", "@test_import_bot /import"]
    ):
        importer = FakeImporter()
        module = make_module(tmp_path / str(index), importer, manual_only=True)
        result = asyncio.run(_dispatch(module, make_command(make_message(), text=text), ManualClient()))
        assert result.status == "handled", text
        assert len(importer.calls) == 1, text


def test_custom_commands_and_reply_texts_take_effect(tmp_path):
    importer = FakeImporter()
    module = make_module(
        tmp_path,
        importer,
        manual_only=True,
        manual_commands=["入库"],
        manual_mention_commands=["入库"],
        sender_ids=["123"],
        sender_usernames=[],
        silent_denied=False,
        reply_denied_text="你不能导入",
        reply_need_reply_text="先回复文件，再发 {command}",
        reply_not_csv_text="这不是 CSV",
        manual_success_text="搞定 {file}：新增 {imported} 更新 {updated} 跳过 {skipped}",
    )
    client = ManualClient()

    # 旧指令不再生效
    assert asyncio.run(_dispatch(module, make_command(make_message(), text="/import"), client)).status == "ignored"

    # 新指令生效，且自定义成功文案被渲染
    cmd = make_command(make_message(), text="入库")
    asyncio.run(_dispatch(module, cmd, client))
    assert len(importer.calls) == 1
    assert client.sent[-1]["text"] == "搞定 x1080x_all_2026-10-09.csv：新增 10 更新 1 跳过 2"

    # 自定义使用提示里的 {command} 会替换成实际指令
    asyncio.run(_dispatch(module, make_command(text="入库"), client))
    assert client.sent[-1]["text"] == "先回复文件，再发 入库"

    # 自定义拒绝文案
    denied = make_command(make_message(), text="入库", sender_id=999)
    asyncio.run(_dispatch(module, denied, client))
    assert client.sent[-1]["text"] == "你不能导入"


def test_oversized_reply_uses_size_placeholders(tmp_path):
    module = make_module(
        tmp_path,
        manual_only=True,
        max_file_size_mb=1,
        reply_oversized_text="{file} 太大：{size} 超过 {max_size}MB",
    )
    client = ManualClient()
    asyncio.run(_dispatch(module, make_command(make_message(size=2 * 1024 * 1024)), client))
    assert client.sent[-1]["text"] == "x1080x_all_2026-10-09.csv 太大：2.0 MB 超过 1MB"


PRECHECK_CSV = (
    "code,magnets,magnet_hash,source_site\n"
    'ABP-975,"ed2k://|file|a.mp4|1|0123456789ABCDEF0123456789ABCDEF|/",ltx-2085,ltx\n'
    'IPX-001,"ed2k://|file|b.mp4|2|FEDCBA9876543210FEDCBA9876543210|/",,ltx\n'
).encode()


def test_precheck_drops_rows_already_in_momo(tmp_path):
    importer = FakeImporter()
    module = make_module(tmp_path, importer)
    module._existing_lookup = lambda: (lambda code, ed2k, site="": code == "ABP-975")
    client = FakeClient(content=PRECHECK_CSV)

    result = asyncio.run(_dispatch(module, make_message(size=len(PRECHECK_CSV)), client))

    assert result.status == "handled"
    assert len(importer.calls) == 1
    uploaded = importer.calls[0]["content"].decode()
    assert "IPX-001" in uploaded
    assert "ABP-975" not in uploaded
    # 占位哈希 ltx-2085 之外的这行也换成了 32 位文件哈希
    assert "fedcba9876543210fedcba9876543210" in uploaded


def test_all_rows_known_skips_upload_and_reports_existing(tmp_path):
    importer = FakeImporter()
    module = make_module(tmp_path, importer, reply_text="已有 {existing} 条，跳过 {skipped}")
    module._existing_lookup = lambda: (lambda code, ed2k, site="": True)
    client = FakeClient(content=PRECHECK_CSV)

    asyncio.run(_dispatch(module, make_message(size=len(PRECHECK_CSV)), client))

    assert importer.calls == []
    assert client.sent[-1]["text"] == "已有 2 条，跳过 2"


def test_default_reply_reports_file_and_upload_stats(tmp_path):
    """默认模板要把「校对」与「导入」两段数字都写清楚，用 total/valid/existing/normalized 占位符。"""
    importer = FakeImporter()
    module = make_module(tmp_path, importer, manual_only=True)
    module._existing_lookup = lambda: (lambda code, ed2k, site="": code == "ABP-975")
    client = ManualClient()
    client.content = PRECHECK_CSV

    asyncio.run(_dispatch(module, make_command(make_message(size=len(PRECHECK_CSV))), client))

    text = client.sent[-1]["text"]
    assert "校对：共 2 行，有效 1 行" in text
    assert "预检：库里已有 1 条" in text
    assert "{total}" not in text and "{existing}" not in text


def test_precheck_error_does_not_block_import(tmp_path):
    importer = FakeImporter()
    module = make_module(tmp_path, importer)

    def boom(code, ed2k, site=""):
        raise RuntimeError("momo 挂了")

    module._existing_lookup = lambda: boom
    client = FakeClient(content=PRECHECK_CSV)

    asyncio.run(_dispatch(module, make_message(size=len(PRECHECK_CSV)), client))

    assert len(importer.calls) == 1
    assert b"IPX-001" in importer.calls[0]["content"]

