from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass
class AdminConfig:
    host: str = "127.0.0.1"
    port: int = 8088
    # 同时配置 username 和 password 时启用 Basic Auth；留空则不鉴权。
    username: str = ""
    password: str = ""


@dataclass
class TelegramConfig:
    enabled: bool = False
    api_id: int | None = None
    api_hash: str = ""
    bot_token: str = ""
    session_name: str = "telegram_bot"
    session_dir: str = "sessions"
    monitored_groups: list[str] = field(default_factory=list)
    monitored_group_meta: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class LogConfig:
    level: str = "INFO"
    dir: str = "runtime/logs"
    file_name: str = "telegram-bot.log"
    max_bytes: int = 5 * 1024 * 1024
    max_files: int = 5
    # 全局日志按天清理：滚动出来的旧文件超过这个天数直接删掉，防止无限增长
    keep_days: int = 30


@dataclass
class RuntimeConfig:
    data_dir: str = "runtime/data"
    tmp_dir: str = "runtime/tmp"


@dataclass
class ChatHistoryConfig:
    enabled: bool = True
    database_path: str = "runtime/data/chat_history.sqlite3"
    max_text_length: int = 16384
    backfill_enabled: bool = False
    backfill_batch_size: int = 500


@dataclass
class ModuleSwitchConfig:
    red_packet: bool = False
    send_red_packet: bool = False
    ai_reply: bool = False
    ai_image: bool = False
    ai_decrypt: bool = False
    gif: bool = False
    checkin: bool = False
    auto_forward: bool = False
    word_cloud: bool = False
    user_profile: bool = False
    quote: bool = False
    chat_games: bool = False
    magnet_import: bool = False
    ltx_watch: bool = False
    site_checkin: bool = False


@dataclass
class RedPacketTemplateConfig:
    id: str
    name: str = ""
    enabled: bool = True
    trigger_keywords: list[str] = field(default_factory=list)
    id_markers: list[str] = field(default_factory=list)
    passphrase_markers: list[str] = field(default_factory=list)
    button_texts: list[str] = field(default_factory=list)
    passphrase: str = ""
    send_message_before_click: bool = False
    delay_before_click: bool = True


@dataclass
class RedPacketConfig:
    templates: list[RedPacketTemplateConfig] = field(default_factory=list)
    send_delay_ms: int = 0
    pre_click_messages: list[str] = field(default_factory=list)


@dataclass
class SendRedPacketTemplateConfig:
    id: str
    name: str = ""
    type: str = "lucky_text"
    enabled: bool = True
    command_prefixes: list[str] = field(default_factory=lambda: ["红包", "/红包"])
    max_packet_count: int = 100


@dataclass
class SendRedPacketConfig:
    templates: list[SendRedPacketTemplateConfig] = field(
        default_factory=lambda: [
            SendRedPacketTemplateConfig(
                id="lucky-text",
                name="拼手气红包",
                type="lucky_text",
                command_prefixes=["红包", "/红包"],
                max_packet_count=100,
            ),
            SendRedPacketTemplateConfig(
                id="math-quiz",
                name="\u8da3\u5473\u7b97\u672f\u9898",
                type="math_quiz",
                command_prefixes=["/math"],
                max_packet_count=1,
            )
        ]
    )
    state_store: str = "runtime/data/send_red_packet_state.json"
    id_length: int = 6
    max_active_packets: int = 20
    min_packet_amount: int = 2
    strict_min_packet_amount: int = 101
    strict_min_packet_group_ids: list[str] = field(default_factory=list)


@dataclass
class AIDecryptTemplateConfig:
    id: str
    name: str = ""
    enabled: bool = True
    trigger_keywords: list[str] = field(default_factory=list)
    id_markers: list[str] = field(default_factory=list)
    require_media: bool = True
    prompt: str = ""


@dataclass
class AIDecryptConfig:
    templates: list[AIDecryptTemplateConfig] = field(
        default_factory=lambda: [
            AIDecryptTemplateConfig(
                id="dice-grid",
                name="九宫格骰子竞猜",
                trigger_keywords=["九宫格骰子竞猜", "目标点数"],
            ),
            AIDecryptTemplateConfig(
                id="qingwa-math-passphrase",
                name="QingWA 数学口令红包",
                trigger_keywords=["请识别图片中的口令", "红包ID", "数学题"],
                id_markers=["红包ID:", "红包ID："],
            ),
            AIDecryptTemplateConfig(
                id="twenty-four-points",
                name="24 点算式",
                trigger_keywords=["24 点开始", "数字：", "可用符号"],
                require_media=False,
            ),
        ]
    )
    timeout_seconds: float = 120.0
    max_tokens: int = 512
    image_detail: str = "original"
    send_delay_ms: int = 0
    state_store: str = "runtime/data/ai_decrypt_claimed_ids.json"


@dataclass
class CheckinTaskConfig:
    id: str
    name: str = ""
    enabled: bool = True
    target: str = ""
    target_title: str = ""
    cron: str = ""
    # Kept so configurations from the former daily-time scheduler still load.
    # The check-in module exposes these values as equivalent cron expressions.
    times: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class CheckinConfig:
    user_session_name: str = ""
    user_id: str = ""
    tasks: list[CheckinTaskConfig] = field(default_factory=list)
    poll_interval_seconds: float = 10.0
    state_store: str = "runtime/data/checkin_state.json"
    catchup_minutes: int = 720
    retry_interval_seconds: float = 300.0
    max_attempts: int = 10
    notify_enabled: bool = True
    notify_bot_token: str = ""
    notify_chat_id: str = ""


@dataclass
class AutoForwardConfig:
    command_prefixes: list[str] = field(default_factory=lambda: [",re"])
    copy_text_command_prefixes: list[str] = field(default_factory=lambda: [",copy"])
    quote_command_prefixes: list[str] = field(default_factory=lambda: [",quote"])
    link_command_prefixes: list[str] = field(default_factory=lambda: [",link"])
    delete_command: bool = True


@dataclass
class MagnetImportConfig:
    manual_only: bool = False
    # 手动模式下可用于触发的指令（精确匹配，大小写不敏感）；@机器人 触发词单独配置
    manual_commands: list[str] = field(default_factory=lambda: ["/import", "/导入"])
    manual_mention_commands: list[str] = field(default_factory=lambda: ["导入", "/import"])
    # 非授权用户（含 @机器人 触发）发指令时：True 完全不回复，只在后台计数
    silent_denied: bool = True
    watch_chats: list[str] = field(default_factory=list)
    sender_usernames: list[str] = field(default_factory=list)
    sender_ids: list[str] = field(default_factory=list)
    file_name_pattern: str = r"^x1080x.*\.csv$"
    max_file_size_mb: int = 2048
    api_base_url: str = "http://127.0.0.1:8022"
    api_username: str = "admin"
    api_password: str = ""
    update_existing: bool = False
    skip_duplicates: bool = True
    reply_text: str = "已自动导入，谢谢老大"
    manual_success_text: str = (
        "✅ 已导入 {file}\n"
        "校对：共 {total} 行，有效 {valid} 行，文件内重复 {dupes} 行，坏行 {invalid} 行；"
        "magnet_hash 改为文件 MD4 {normalized} 行\n"
        "预检：库里已有 {existing} 条，未上传\n"
        "导入：新增 {imported}，跳过 {skipped}，更新 {updated}，错误 {errors}"
    )
    reply_denied_text: str = "仅群管理员或已授权用户可以触发导入。"
    reply_need_reply_text: str = "请回复一条 CSV 文件消息，再发送 {command}。"
    reply_not_csv_text: str = "回复的消息必须是符合文件名规则的 CSV 文件。"
    reply_duplicate_text: str = "这份文件正在处理或已经处理过，请查看导入记录。"
    reply_oversized_text: str = "文件超过 {max_size} MB，无法导入。"
    reply_missing_credentials_text: str = "尚未配置 momo 登录信息，请联系管理员。"
    reply_failed_text: str = "导入失败，请在管理后台查看错误详情，修复后可再次回复触发。"
    download_dir: str = "runtime/tmp/magnet_import"
    download_retries: int = 5
    import_timeout_seconds: float = 1800.0
    state_store: str = "runtime/data/magnet_import_state.json"


@dataclass
class LtxWatchConfig:
    """LTX 频道自动采集：用独立用户会话读频道，解析 ed2k 后导入 momo。"""

    dry_run: bool = True
    # 默认留空：不把任何具体频道写死，部署后自行填写「监控频道」
    channel: str = ""
    session_name: str = "ltx_user"
    poll_interval_seconds: int = 300
    initial_full_scan: bool = True
    max_initial_messages: int = 3000
    fetch_metadata: bool = True
    metadata_retries: int = 3
    metadata_delay_seconds: int = 10
    # 可选：JavDB 被 Cloudflare 拦时，用 FlareSolverr 代取页面（填 http://ip:8191）
    metadata_flaresolverr: str = ""
    # 元数据抓取顺序（用户指定）：DMM → AVbase → AVWikiDB → Jav321 → MGStage → JavDB。
    # JavDB 现在部分页面要登录才能看，默认不放在链里（要启用就把 "javdb" 加回列表）。
    metadata_sources: list[str] = field(
        default_factory=lambda: ["dmm", "avbase", "avwikidb", "jav321", "mgstage"]
    )
    source_site: str = "ltx"
    forum_name: str = "LTX破解"
    max_pending_days: int = 7
    # 回补专用：连续 N 条都已存在就停止往前扫（0 = 不启用，普通部署保持原逻辑）
    backfill_stop_after: int = 0
    # 是否允许 FC2 作品写入 momo（关闭时只展示，且不补封面/演员）
    fc2_import: bool = False
    notify_enabled: bool = False
    notify_cron: str = "0 21 * * *"
    notify_target: str = "me"
    # 可选：用自己的通知 bot 直接发（填 Bot Token 与接收用的 chat_id）。
    # 这样汇总会以该 bot 的身份出现在对话里，和它平时的推送一致，也不会触发任何回复。
    notify_bot_token: str = ""
    notify_chat_id: str = ""
    notify_text: str = (
        "📥 LTX 采集汇总（{since} ~ {now}）\n"
        "新增磁链 {imported} 条 · 跳过重复 {skipped} 条 · 失败 {failed} 条 · 待补封面 {pending} 条"
    )
    state_store: str = "runtime/data/ltx_watch_state.json"


@dataclass
class SiteCheckinSiteConfig:
    """单个站点的签到配置（PT 站 / 论坛）。新增站点只改这里，不用重新打包。"""

    id: str
    name: str = ""
    enabled: bool = True
    # 签到页地址，例如 https://ourbits.club/attendance.php
    url: str = ""
    # 浏览器里复制的整段 Cookie
    cookie: str = ""
    cron: str = "10 8 * * *"
    # auto=自动识别（GET / 表单 POST / 滑动认证），get=只取页面，post=只提交
    method: str = "auto"
    # 需要直接提交时用；留空则自动从页面表单里找
    sign_url: str = ""
    # 额外 POST 参数，形如 a=1&b=2
    post_data: str = ""
    # 额外请求头，形如 Referer:https://x/;X-Requested-With:XMLHttpRequest
    headers: str = ""
    # 该站点的成功/已签关键字，逗号分隔；留空用内置判定
    success_keywords: str = ""
    already_keywords: str = ""
    note: str = ""


@dataclass
class SiteCheckinConfig:
    """网页签到：按 Cookie 直接请求签到接口，不依赖浏览器。"""

    enabled: bool = False
    poll_interval_seconds: int = 30
    timeout_seconds: int = 40
    # 日志保留天数，超期自动清理
    log_keep_days: int = 30
    # 需要过 Cloudflare 时填 FlareSolverr 地址（http://ip:8191）
    flaresolverr: str = ""
    notify_enabled: bool = True
    notify_bot_token: str = ""
    notify_chat_id: str = ""
    notify_text: str = "站点签到 · {name}\n{status}：{reason}\n时间：{time}"
    sites: list[SiteCheckinSiteConfig] = field(default_factory=list)
    state_store: str = "runtime/data/site_checkin_state.json"


@dataclass
class WordCloudConfig:
    command_prefixes: list[str] = field(default_factory=lambda: ["cy", "/cy"])
    default_limit: int = 1000
    max_limit: int = 2000
    min_limit: int = 50
    output_dir: str = "runtime/data/generated_word_clouds"
    width: int = 900
    height: int = 640
    delete_command: bool = True
    ignore_command_prefixes: list[str] = field(default_factory=lambda: ["/", ".", ","])
    stop_words: list[str] = field(default_factory=list)


@dataclass
class UserProfileConfig:
    command_prefixes: list[str] = field(default_factory=lambda: ["/profile", "/fx", ".fx", "画像", "用户画像"])
    default_sample_size: int = 40
    min_sample_size: int = 10
    max_sample_size: int = 2000
    min_required_samples: int = 8
    timeout_seconds: float = 180.0
    max_tokens: int = 4000
    # 保留字段以兼容已有配置；画像命令现在始终原地更新为进度和结果。
    delete_command: bool = False
    style_prompt: str = (
        "像熟悉群聊黑话的损友一样写：毒舌、有梗、敢吐槽，允许适度夸张、反差和好笑的外号，"
        "少说四平八稳的套话。每个笑点仍要能在样本中找到依据；可以损，但不要恶意羞辱、攻击外貌，"
        "也不要触碰敏感属性或把娱乐性观察写成事实诊断。"
    )


@dataclass
class QuoteConfig:
    command_prefixes: list[str] = field(default_factory=lambda: ["/q", "语录"])
    api_url: str = "https://quote-api-enhanced.zhetengsha.eu.org/generate.webp"
    background_color: str = "#1b1429"
    width: int = 512
    height: int = 768
    scale: int = 2
    emoji_brand: str = "apple"
    max_messages: int = 5
    timeout_seconds: float = 60.0
    sticker_set_short_name: str = ""
    tmp_dir: str = "runtime/tmp"












@dataclass
class LuckyWordConfig:
    enabled: bool = True
    words: list[str] = field(
        default_factory=lambda: [
            "显卡",
            "咖啡",
            "服务器",
            "黑苹果",
            "回血",
            "加速",
            "摸鱼",
            "奶茶",
            "硬盘",
            "截图",
            "部署",
            "重启",
        ]
    )
    reward_text: str = "+100"
    daily_limit_per_chat: int = 1
    per_user_daily_limit: int = 1
    ignore_command_prefixes: list[str] = field(default_factory=lambda: ["/", ".", ",", "+"])
    min_message_length: int = 2


@dataclass
class EmojiRiddleItemConfig:
    clue: str
    answers: list[str] = field(default_factory=list)
    hint: str = ""


@dataclass
class TimedImageGameConfig:
    enabled: bool = True
    command_prefixes: list[str] = field(default_factory=list)
    duration_seconds: float = 20.0
    reward_text: str = "+100"
    winner_policy: str = "first"
    announce_answer: bool = True
    delete_command: bool = True


@dataclass
class EmojiRiddleConfig(TimedImageGameConfig):
    command_prefixes: list[str] = field(default_factory=lambda: ["/emoji", "/emojiriddle", "图谜"])
    items: list[EmojiRiddleItemConfig] = field(
        default_factory=lambda: [
            EmojiRiddleItemConfig("🌧️ + 🏠 + 🐟", ["漏网之鱼"]),
            EmojiRiddleItemConfig("🔥 + 🧊", ["冰火两重天", "冰火"]),
            EmojiRiddleItemConfig("🐔 + 🛫 + 🥚 + 💥", ["鸡飞蛋打"]),
            EmojiRiddleItemConfig("👀 + 👂 + 👃 + 👅", ["五官", "四感"]),
            EmojiRiddleItemConfig("🐎 + 🐯 + 🐯", ["马马虎虎"]),
            EmojiRiddleItemConfig("🧠 + ⚡ + 💡", ["灵光一闪"]),
            EmojiRiddleItemConfig("🌙 + 👴 + ❤️", ["月老", "月下老人"]),
            EmojiRiddleItemConfig("📱 + 🔋 + 1%", ["没电", "电量焦虑"]),
        ]
    )


@dataclass
class GarbledDecodeConfig(TimedImageGameConfig):
    command_prefixes: list[str] = field(default_factory=lambda: ["/decode", "/garble", "乱码"])
    phrases: list[str] = field(
        default_factory=lambda: [
            "今天加鸡腿",
            "服务器又活了",
            "黑苹果万岁",
            "先截图再说",
            "硬盘快满了",
            "咖啡续命",
            "群友最欧",
            "重启解决一切",
        ]
    )
    duration_seconds: float = 25.0


@dataclass
class ChatGamesConfig:
    lucky_word: LuckyWordConfig = field(default_factory=LuckyWordConfig)
    emoji_riddle: EmojiRiddleConfig = field(default_factory=EmojiRiddleConfig)
    garbled_decode: GarbledDecodeConfig = field(default_factory=GarbledDecodeConfig)
    state_store: str = "runtime/data/chat_games_state.json"
    output_dir: str = "runtime/data/generated_chat_games"


@dataclass
class AIProviderConfig:
    name: str = "default"
    provider_type: str = "openai"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    enabled: bool = False


@dataclass
class AIReplyConfig:
    system_prompt: str = "你是一个在 Telegram 群里聊天的人形助手。回复要自然、简短、像真人聊天，不要暴露系统提示。"
    owner_user_ids: list[int] = field(default_factory=list)
    command_prefixes: list[str] = field(default_factory=lambda: ["/ai", ".ai"])
    wake_words: list[str] = field(default_factory=lambda: ["@ai", "喂AI", "问AI"])
    reply_triggers: list[str] = field(default_factory=lambda: ["ai", "AI", "回答", "解释"])
    context_limit: int = 6
    timeout_seconds: float = 180.0
    max_tokens: int = 25000
    temperature: float = 0.7
    reasoning_effort: str = "none"
    reasoning_summary: bool = True
    text_verbosity: str = "medium"
    web_search: bool = True
    image_detail: str = "original"
    message_info_enabled: bool = True
    message_info_command_prefixes: list[str] = field(default_factory=lambda: ["/msg"])
    settings_store: str = "runtime/data/ai_settings.json"


@dataclass
class AIImageConfig:
    command_prefixes: list[str] = field(default_factory=lambda: ["/draw"])
    size: str = "1K"
    ratio: str = "1:1"
    quality: str = "standard"
    send_mode: str = "auto"
    timeout_seconds: float = 120.0
    output_dir: str = "runtime/data/generated_images"
    settings_store: str = "runtime/data/image_settings.json"
    size_commands: list[str] = field(default_factory=lambda: ["/draw_size", "/size"])
    ratio_commands: list[str] = field(default_factory=lambda: ["/draw_ratio", "/ratio"])
    send_mode_commands: list[str] = field(default_factory=lambda: ["/send_mode", "/draw_send", "/image_send"])
    model_commands: list[str] = field(default_factory=lambda: ["/draw_model", "/image_model", "/img_model"])


@dataclass
class GifConfig:
    command_prefixes: list[str] = field(default_factory=lambda: ["/vgif", "动图", "视频动图", "转动图"])
    output_dir: str = "runtime/data/generated_gifs"
    max_seconds: float = 8.0
    fps: int = 12
    width: str = "auto"
    max_bytes: int = 8 * 1024 * 1024


@dataclass
class AIConfig:
    reply_provider: AIProviderConfig = field(default_factory=AIProviderConfig)
    image_provider: AIProviderConfig = field(default_factory=lambda: AIProviderConfig(name="image"))
    reply: AIReplyConfig = field(default_factory=AIReplyConfig)
    image: AIImageConfig = field(default_factory=AIImageConfig)


@dataclass
class AppConfig:
    project_name: str = "TelegramBot"
    version: str = "0.1.0"
    admin: AdminConfig = field(default_factory=AdminConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    logs: LogConfig = field(default_factory=LogConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    chat_history: ChatHistoryConfig = field(default_factory=ChatHistoryConfig)
    modules: ModuleSwitchConfig = field(default_factory=ModuleSwitchConfig)
    red_packet: RedPacketConfig = field(default_factory=RedPacketConfig)
    send_red_packet: SendRedPacketConfig = field(default_factory=SendRedPacketConfig)
    ai_decrypt: AIDecryptConfig = field(default_factory=AIDecryptConfig)
    gif: GifConfig = field(default_factory=GifConfig)
    checkin: CheckinConfig = field(default_factory=CheckinConfig)
    auto_forward: AutoForwardConfig = field(default_factory=AutoForwardConfig)
    word_cloud: WordCloudConfig = field(default_factory=WordCloudConfig)
    user_profile: UserProfileConfig = field(default_factory=UserProfileConfig)
    quote: QuoteConfig = field(default_factory=QuoteConfig)
    chat_games: ChatGamesConfig = field(default_factory=ChatGamesConfig)
    magnet_import: MagnetImportConfig = field(default_factory=MagnetImportConfig)
    ltx_watch: LtxWatchConfig = field(default_factory=LtxWatchConfig)
    site_checkin: SiteCheckinConfig = field(default_factory=SiteCheckinConfig)
    ai: AIConfig = field(default_factory=AIConfig)

    @classmethod
    def defaults_dict(cls) -> dict[str, Any]:
        return asdict(cls())

    @classmethod
    def ensure_file(cls, path: Path) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(cls.defaults_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        base = cls.defaults_dict()
        if not path.exists():
            cls.ensure_file(path)
        with path.open("r", encoding="utf-8") as fp:
            local = json.load(fp)
        merged = _deep_merge(base, local)
        _apply_env_overrides(merged)
        return cls.from_dict(merged)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        return cls(
            project_name=data.get("project_name", cls.project_name),
            version=data.get("version", cls.version),
            admin=AdminConfig(**data.get("admin", {})),
            telegram=TelegramConfig(**data.get("telegram", {})),
            logs=LogConfig(**data.get("logs", {})),
            runtime=RuntimeConfig(**data.get("runtime", {})),
            chat_history=chat_history_config_from_dict(data.get("chat_history", {})),
            modules=ModuleSwitchConfig(**data.get("modules", {})),
            red_packet=RedPacketConfig(
                templates=[
                    RedPacketTemplateConfig(**template)
                    for template in data.get("red_packet", {}).get("templates", [])
                    if isinstance(template, dict)
                ],
                send_delay_ms=int(data.get("red_packet", {}).get("send_delay_ms") or 0),
                pre_click_messages=_clean_string_list(
                    data.get("red_packet", {}).get("pre_click_messages")
                ),
            ),
            send_red_packet=_send_red_packet_config_from_dict(data.get("send_red_packet", {})),
            ai_decrypt=_ai_decrypt_config_from_dict(data.get("ai_decrypt", {})),
            checkin=CheckinConfig(
                user_session_name=str(data.get("checkin", {}).get("user_session_name") or ""),
                user_id=str(data.get("checkin", {}).get("user_id") or ""),
                tasks=[
                    CheckinTaskConfig(**task)
                    for task in data.get("checkin", {}).get("tasks", [])
                    if isinstance(task, dict)
                ],
                poll_interval_seconds=float(data.get("checkin", {}).get("poll_interval_seconds") or 10.0),
                state_store=str(data.get("checkin", {}).get("state_store") or "runtime/data/checkin_state.json"),
                catchup_minutes=_int_or_default(data.get("checkin", {}).get("catchup_minutes"), 720),
                retry_interval_seconds=float(
                    data.get("checkin", {}).get("retry_interval_seconds") or 300.0
                ),
                max_attempts=_int_or_default(data.get("checkin", {}).get("max_attempts"), 10),
                notify_enabled=bool(data.get("checkin", {}).get("notify_enabled", True)),
                notify_bot_token=str(data.get("checkin", {}).get("notify_bot_token") or ""),
                notify_chat_id=str(data.get("checkin", {}).get("notify_chat_id") or ""),
            ),
            auto_forward=_auto_forward_config_from_dict(data.get("auto_forward", {})),
            word_cloud=_word_cloud_config_from_dict(data.get("word_cloud", {})),
            user_profile=user_profile_config_from_dict(data.get("user_profile", {})),
            quote=_quote_config_from_dict(data.get("quote", {})),
            chat_games=_chat_games_config_from_dict(data.get("chat_games", {})),
            magnet_import=magnet_import_config_from_dict(data.get("magnet_import", {})),
            ltx_watch=ltx_watch_config_from_dict(data.get("ltx_watch", {})),
            site_checkin=site_checkin_config_from_dict(data.get("site_checkin", {})),
            gif=GifConfig(**data.get("gif", {})),
            ai=AIConfig(
                reply_provider=AIProviderConfig(**data.get("ai", {}).get("reply_provider", {})),
                image_provider=AIProviderConfig(**data.get("ai", {}).get("image_provider", {})),
                reply=AIReplyConfig(**data.get("ai", {}).get("reply", {})),
                image=AIImageConfig(**data.get("ai", {}).get("image", {})),
            ),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for provider in (data["ai"]["reply_provider"], data["ai"]["image_provider"]):
            if provider.get("api_key"):
                provider["api_key"] = _mask_secret(provider["api_key"])
        if data["telegram"].get("api_hash"):
            data["telegram"]["api_hash"] = _mask_secret(data["telegram"]["api_hash"])
        if data["telegram"].get("bot_token"):
            data["telegram"]["bot_token"] = _mask_secret(data["telegram"]["bot_token"])
        if data["admin"].get("password"):
            data["admin"]["password"] = _mask_secret(data["admin"]["password"])
        if data["magnet_import"].get("api_password"):
            data["magnet_import"]["api_password"] = _mask_secret(data["magnet_import"]["api_password"])
        return data


def _apply_env_overrides(config: dict[str, Any]) -> None:
    env_map = {
        "TGBOT_ADMIN_HOST": ("admin", "host"),
        "TGBOT_ADMIN_PORT": ("admin", "port"),
        "TGBOT_ADMIN_USERNAME": ("admin", "username"),
        "TGBOT_ADMIN_PASSWORD": ("admin", "password"),
        "TGBOT_TELEGRAM_API_ID": ("telegram", "api_id"),
        "TGBOT_TELEGRAM_API_HASH": ("telegram", "api_hash"),
        "TGBOT_TELEGRAM_BOT_TOKEN": ("telegram", "bot_token"),
        "TGBOT_TELEGRAM_ENABLED": ("telegram", "enabled"),
        "TGBOT_LOG_LEVEL": ("logs", "level"),
    }
    for env_name, path in env_map.items():
        raw = os.getenv(env_name)
        if raw is None:
            continue
        section, key = path
        config[section][key] = _coerce_env_value(raw)


def _coerce_env_value(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "1", "yes", "on"}:
        return True
    if lowered in {"false", "0", "no", "off"}:
        return False
    try:
        return int(value)
    except ValueError:
        return value


def chat_history_config_from_dict(data: Any) -> ChatHistoryConfig:
    defaults = ChatHistoryConfig()
    if not isinstance(data, dict):
        return defaults
    return ChatHistoryConfig(
        enabled=bool(data.get("enabled", defaults.enabled)),
        database_path=str(data.get("database_path") or defaults.database_path).strip() or defaults.database_path,
        max_text_length=max(
            1024,
            min(int(data.get("max_text_length") or defaults.max_text_length), 65536),
        ),
        backfill_enabled=bool(data.get("backfill_enabled", defaults.backfill_enabled)),
        backfill_batch_size=max(
            50,
            min(int(data.get("backfill_batch_size") or defaults.backfill_batch_size), 5000),
        ),
    )


def _ai_decrypt_config_from_dict(data: Any) -> AIDecryptConfig:
    defaults = AIDecryptConfig()
    if not isinstance(data, dict):
        return defaults
    raw_templates = data.get("templates")
    templates = defaults.templates
    if isinstance(raw_templates, list):
        templates = [
            AIDecryptTemplateConfig(**template)
            for template in raw_templates
            if isinstance(template, dict)
        ]
    templates = _ensure_ai_decrypt_builtin_templates(templates)
    return AIDecryptConfig(
        templates=templates,
        timeout_seconds=float(data.get("timeout_seconds") or defaults.timeout_seconds),
        max_tokens=int(data.get("max_tokens") or defaults.max_tokens),
        image_detail=str(data.get("image_detail") or defaults.image_detail),
        send_delay_ms=int(data.get("send_delay_ms") or defaults.send_delay_ms),
        state_store=str(data.get("state_store") or defaults.state_store),
    )


def _ensure_ai_decrypt_builtin_templates(
    templates: list[AIDecryptTemplateConfig],
) -> list[AIDecryptTemplateConfig]:
    if any(template.id == "twenty-four-points" for template in templates):
        return templates
    return [
        *templates,
        AIDecryptTemplateConfig(
            id="twenty-four-points",
            name="24 点算式",
            trigger_keywords=["24 点开始", "数字：", "可用符号"],
            require_media=False,
        ),
    ]


def _send_red_packet_config_from_dict(data: Any) -> SendRedPacketConfig:
    defaults = SendRedPacketConfig()
    if not isinstance(data, dict):
        return defaults
    templates = defaults.templates
    if isinstance(data.get("templates"), list):
        templates = [
            _send_red_packet_template_from_dict(item)
            for item in data["templates"]
            if isinstance(item, dict)
        ]
    elif isinstance(data.get("command_prefixes"), list):
        templates = [
            SendRedPacketTemplateConfig(
                id="lucky-text",
                name="拼手气口令红包",
                type="lucky_text",
                command_prefixes=[str(item).strip() for item in data["command_prefixes"] if str(item).strip()],
                max_packet_count=max(1, min(int(data.get("max_packet_count") or 100), 1000)),
            )
        ]
    templates = _ensure_send_red_packet_builtin_templates(templates)
    return SendRedPacketConfig(
        templates=templates,
        state_store=str(data.get("state_store") or defaults.state_store),
        id_length=max(4, min(int(data.get("id_length") or defaults.id_length), 12)),
        max_active_packets=max(1, min(int(data.get("max_active_packets") or defaults.max_active_packets), 200)),
        min_packet_amount=max(2, min(int(data.get("min_packet_amount") or defaults.min_packet_amount), 1_000_000)),
        strict_min_packet_amount=max(
            2,
            min(int(data.get("strict_min_packet_amount") or defaults.strict_min_packet_amount), 1_000_000),
        ),
        strict_min_packet_group_ids=[
            str(item).strip()
            for item in data.get("strict_min_packet_group_ids", defaults.strict_min_packet_group_ids)
            if str(item).strip()
        ]
        if isinstance(data.get("strict_min_packet_group_ids", defaults.strict_min_packet_group_ids), list)
        else list(defaults.strict_min_packet_group_ids),
    )


def _send_red_packet_template_from_dict(data: dict[str, Any]) -> SendRedPacketTemplateConfig:
    return SendRedPacketTemplateConfig(
        id=str(data.get("id") or "").strip(),
        name=str(data.get("name") or "").strip(),
        type=str(data.get("type") or "lucky_text").strip(),
        enabled=bool(data.get("enabled", True)),
        command_prefixes=[str(item).strip() for item in data.get("command_prefixes", []) if str(item).strip()]
        if isinstance(data.get("command_prefixes"), list)
        else [],
        max_packet_count=max(1, min(int(data.get("max_packet_count") or 100), 1000)),
    )


def _ensure_send_red_packet_builtin_templates(
    templates: list[SendRedPacketTemplateConfig],
) -> list[SendRedPacketTemplateConfig]:
    if any(template.type == "math_quiz" for template in templates):
        return templates
    return [
        *templates,
        SendRedPacketTemplateConfig(
            id="math-quiz",
            name="\u8da3\u5473\u7b97\u672f\u9898",
            type="math_quiz",
            command_prefixes=["/math"],
            max_packet_count=1,
        ),
    ]


def _auto_forward_config_from_dict(data: Any) -> AutoForwardConfig:
    defaults = AutoForwardConfig()
    if not isinstance(data, dict):
        return defaults
    prefixes = _list_or_default(data.get("command_prefixes"), defaults.command_prefixes)
    copy_prefixes = _list_or_default(data.get("copy_text_command_prefixes"), defaults.copy_text_command_prefixes)
    quote_prefixes = _list_or_default(data.get("quote_command_prefixes"), defaults.quote_command_prefixes)
    link_prefixes = _list_or_default(data.get("link_command_prefixes"), defaults.link_command_prefixes)
    return AutoForwardConfig(
        command_prefixes=prefixes,
        copy_text_command_prefixes=copy_prefixes,
        quote_command_prefixes=quote_prefixes,
        link_command_prefixes=link_prefixes,
        delete_command=bool(data.get("delete_command", defaults.delete_command)),
    )


def magnet_import_config_from_dict(data: Any) -> MagnetImportConfig:
    defaults = MagnetImportConfig()
    if not isinstance(data, dict):
        return defaults

    def _int(key: str, low: int, high: int) -> int:
        try:
            value = int(data.get(key, getattr(defaults, key)))
        except (TypeError, ValueError):
            value = getattr(defaults, key)
        return max(low, min(high, value))

    try:
        timeout = float(data.get("import_timeout_seconds", defaults.import_timeout_seconds))
    except (TypeError, ValueError):
        timeout = defaults.import_timeout_seconds

    def _text(key: str) -> str:
        # 留空表示恢复默认文案，避免后台误清空后机器人彻底不回复
        return str(data.get(key) or getattr(defaults, key))

    def _commands(key: str) -> list[str]:
        raw = data.get(key)
        values = _clean_string_list(raw) if isinstance(raw, list) else []
        unique: list[str] = []
        for item in values:
            if item not in unique:
                unique.append(item)
        return unique or list(getattr(defaults, key))

    watch_chats = data.get("watch_chats")
    return MagnetImportConfig(
        manual_only=bool(data.get("manual_only", defaults.manual_only)),
        manual_commands=_commands("manual_commands"),
        manual_mention_commands=_commands("manual_mention_commands"),
        silent_denied=bool(data.get("silent_denied", defaults.silent_denied)),
        watch_chats=_clean_string_list(watch_chats) if isinstance(watch_chats, list) else list(defaults.watch_chats),
        sender_usernames=[
            item.lstrip("@") for item in _clean_string_list(data.get("sender_usernames", defaults.sender_usernames))
        ],
        sender_ids=_clean_string_list(data.get("sender_ids", defaults.sender_ids)),
        file_name_pattern=str(data.get("file_name_pattern") or defaults.file_name_pattern).strip(),
        max_file_size_mb=_int("max_file_size_mb", 1, 4096),
        api_base_url=str(data.get("api_base_url") or defaults.api_base_url).strip().rstrip("/"),
        api_username=str(data.get("api_username", defaults.api_username) or "").strip(),
        api_password=str(data.get("api_password", defaults.api_password) or ""),
        # momo 导入接口没有按行更新能力：update_existing=true 只会再新增一条重复，
        # 也不能关掉「跳过重复」。两个开关从面板移除，这里固定成唯一正确取值。
        update_existing=False,
        skip_duplicates=True,
        reply_text=_text("reply_text"),
        manual_success_text=_text("manual_success_text"),
        reply_denied_text=_text("reply_denied_text"),
        reply_need_reply_text=_text("reply_need_reply_text"),
        reply_not_csv_text=_text("reply_not_csv_text"),
        reply_duplicate_text=_text("reply_duplicate_text"),
        reply_oversized_text=_text("reply_oversized_text"),
        reply_missing_credentials_text=_text("reply_missing_credentials_text"),
        reply_failed_text=_text("reply_failed_text"),
        download_dir=str(data.get("download_dir") or defaults.download_dir).strip(),
        download_retries=_int("download_retries", 1, 20),
        import_timeout_seconds=max(30.0, min(7200.0, timeout)),
        state_store=str(data.get("state_store") or defaults.state_store).strip(),
    )


def _cron_or_default(value: Any, default: str) -> str:
    """校验 5 段 cron；不合法时回落到默认，避免面板填错就再也不发通知。"""
    text = str(value or "").strip()
    if not text:
        return default
    parts = text.split()
    if len(parts) != 5 or not all(re.fullmatch(r"[\d*/,\-\s]+", part) and part for part in parts):
        return default
    return text


def _int_or_default(value: Any, default: int) -> int:
    """整数解析，非法值回落到默认（面板里留空或填字母都不该把模块搞崩）。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def ltx_watch_config_from_dict(data: Any) -> LtxWatchConfig:
    defaults = LtxWatchConfig()
    if not isinstance(data, dict):
        return defaults

    def _bool(key: str) -> bool:
        return bool(data.get(key, getattr(defaults, key)))

    def _int(key: str, low: int, high: int) -> int:
        try:
            value = int(data.get(key, getattr(defaults, key)))
        except (TypeError, ValueError):
            value = getattr(defaults, key)
        return max(low, min(high, value))

    def _sources() -> list[str]:
        raw = data.get("metadata_sources")
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.replace(">", ",").split(",")]
        if not isinstance(raw, list):
            return list(defaults.metadata_sources)
        cleaned = [str(item).strip().lower() for item in raw if str(item).strip()]
        return cleaned or list(defaults.metadata_sources)

    return LtxWatchConfig(
        dry_run=_bool("dry_run"),
        channel=str(data.get("channel") or defaults.channel).strip().lstrip("@"),
        session_name=str(data.get("session_name") or defaults.session_name).strip(),
        poll_interval_seconds=_int("poll_interval_seconds", 60, 3600),
        initial_full_scan=_bool("initial_full_scan"),
        max_initial_messages=_int("max_initial_messages", 20, 50000),
        fetch_metadata=_bool("fetch_metadata"),
        metadata_retries=_int("metadata_retries", 0, 5),
        metadata_delay_seconds=_int("metadata_delay_seconds", 0, 120),
        metadata_flaresolverr=str(data.get("metadata_flaresolverr") or "").strip(),
        metadata_sources=_sources(),
        source_site=str(data.get("source_site") or defaults.source_site).strip() or defaults.source_site,
        forum_name=str(data.get("forum_name") or defaults.forum_name).strip() or defaults.forum_name,
        max_pending_days=_int("max_pending_days", 1, 90),
        backfill_stop_after=_int("backfill_stop_after", 0, 1000),
        fc2_import=bool(data.get("fc2_import", False)),
        notify_enabled=_bool("notify_enabled"),
        notify_cron=_cron_or_default(data.get("notify_cron"), defaults.notify_cron),
        notify_target=str(data.get("notify_target") or defaults.notify_target).strip() or defaults.notify_target,
        notify_bot_token=str(data.get("notify_bot_token") or "").strip(),
        notify_chat_id=str(data.get("notify_chat_id") or "").strip(),
        notify_text=str(data.get("notify_text") or defaults.notify_text),
        state_store=str(data.get("state_store") or defaults.state_store).strip(),
    )


def site_checkin_config_from_dict(data: Any) -> SiteCheckinConfig:
    """解析站点签到配置。站点列表完全由数据驱动，新增/删除站点都不需要重新打包。"""
    defaults = SiteCheckinConfig()
    if not isinstance(data, dict):
        return defaults

    def _int(key: str, low: int, high: int) -> int:
        try:
            value = int(data.get(key, getattr(defaults, key)))
        except (TypeError, ValueError):
            value = getattr(defaults, key)
        return max(low, min(high, value))

    raw_sites = data.get("sites")
    sites: list[SiteCheckinSiteConfig] = []
    if isinstance(raw_sites, list):
        for index, item in enumerate(raw_sites, start=1):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            site_id = str(item.get("id") or "").strip() or f"site{index}"
            if not url:
                continue
            sites.append(
                SiteCheckinSiteConfig(
                    id=site_id,
                    name=str(item.get("name") or site_id).strip(),
                    enabled=bool(item.get("enabled", True)),
                    url=url,
                    cookie=str(item.get("cookie") or "").strip(),
                    cron=_cron_or_default(item.get("cron"), "10 8 * * *"),
                    method=str(item.get("method") or "auto").strip().lower() or "auto",
                    sign_url=str(item.get("sign_url") or "").strip(),
                    post_data=str(item.get("post_data") or "").strip(),
                    headers=str(item.get("headers") or "").strip(),
                    success_keywords=str(item.get("success_keywords") or "").strip(),
                    already_keywords=str(item.get("already_keywords") or "").strip(),
                    note=str(item.get("note") or "").strip(),
                )
            )

    return SiteCheckinConfig(
        enabled=bool(data.get("enabled", defaults.enabled)),
        poll_interval_seconds=_int("poll_interval_seconds", 10, 3600),
        timeout_seconds=_int("timeout_seconds", 10, 180),
        log_keep_days=_int("log_keep_days", 1, 365),
        flaresolverr=str(data.get("flaresolverr") or "").strip(),
        notify_enabled=bool(data.get("notify_enabled", defaults.notify_enabled)),
        notify_bot_token=str(data.get("notify_bot_token") or "").strip(),
        notify_chat_id=str(data.get("notify_chat_id") or "").strip(),
        notify_text=str(data.get("notify_text") or defaults.notify_text),
        sites=sites,
        state_store=str(data.get("state_store") or defaults.state_store).strip(),
    )


def _word_cloud_config_from_dict(data: Any) -> WordCloudConfig:
    defaults = WordCloudConfig()
    if not isinstance(data, dict):
        return defaults
    max_limit = max(50, min(int(data.get("max_limit") or defaults.max_limit), 10000))
    min_limit = max(10, min(int(data.get("min_limit") or defaults.min_limit), max_limit))
    return WordCloudConfig(
        command_prefixes=_list_or_default(data.get("command_prefixes"), defaults.command_prefixes),
        default_limit=max(min_limit, min(int(data.get("default_limit") or defaults.default_limit), max_limit)),
        max_limit=max_limit,
        min_limit=min_limit,
        output_dir=str(data.get("output_dir") or defaults.output_dir),
        width=max(480, min(int(data.get("width") or defaults.width), 2400)),
        height=max(360, min(int(data.get("height") or defaults.height), 1800)),
        delete_command=bool(data.get("delete_command", defaults.delete_command)),
        ignore_command_prefixes=_list_or_default(data.get("ignore_command_prefixes"), defaults.ignore_command_prefixes),
        stop_words=_list_or_default(data.get("stop_words"), defaults.stop_words),
    )


def user_profile_config_from_dict(data: Any) -> UserProfileConfig:
    defaults = UserProfileConfig()
    if not isinstance(data, dict):
        return defaults
    max_sample_size = max(10, min(int(data.get("max_sample_size") or defaults.max_sample_size), 2000))
    min_sample_size = max(3, min(int(data.get("min_sample_size") or defaults.min_sample_size), max_sample_size))
    default_sample_size = max(
        min_sample_size,
        min(int(data.get("default_sample_size") or defaults.default_sample_size), max_sample_size),
    )
    min_required_samples = max(
        3,
        min(int(data.get("min_required_samples") or defaults.min_required_samples), default_sample_size),
    )
    return UserProfileConfig(
        command_prefixes=_list_or_default(data.get("command_prefixes"), defaults.command_prefixes),
        default_sample_size=default_sample_size,
        min_sample_size=min_sample_size,
        max_sample_size=max_sample_size,
        min_required_samples=min_required_samples,
        timeout_seconds=max(5.0, min(float(data.get("timeout_seconds") or defaults.timeout_seconds), 600.0)),
        max_tokens=max(512, min(int(data.get("max_tokens") or defaults.max_tokens), 12000)),
        delete_command=bool(data.get("delete_command", defaults.delete_command)),
        style_prompt=str(data.get("style_prompt") or defaults.style_prompt).strip() or defaults.style_prompt,
    )


def _quote_config_from_dict(data: Any) -> QuoteConfig:
    defaults = QuoteConfig()
    if not isinstance(data, dict):
        return defaults
    return QuoteConfig(
        command_prefixes=_list_or_default(data.get("command_prefixes"), defaults.command_prefixes),
        api_url=str(data.get("api_url") or defaults.api_url),
        background_color=str(data.get("background_color") or defaults.background_color),
        width=max(256, min(int(data.get("width") or defaults.width), 2048)),
        height=max(256, min(int(data.get("height") or defaults.height), 4096)),
        scale=max(1, min(int(data.get("scale") or defaults.scale), 4)),
        emoji_brand=str(data.get("emoji_brand") or defaults.emoji_brand),
        max_messages=max(1, min(int(data.get("max_messages") or defaults.max_messages), 10)),
        timeout_seconds=max(5.0, min(float(data.get("timeout_seconds") or defaults.timeout_seconds), 300.0)),
        sticker_set_short_name=str(data.get("sticker_set_short_name") or ""),
        tmp_dir=str(data.get("tmp_dir") or defaults.tmp_dir),
    )


def _lucky_word_config_from_dict(data: Any) -> LuckyWordConfig:
    defaults = LuckyWordConfig()
    if not isinstance(data, dict):
        return defaults
    return LuckyWordConfig(
        enabled=bool(data.get("enabled", defaults.enabled)),
        words=_list_or_default(data.get("words"), defaults.words),
        reward_text=str(data.get("reward_text") or defaults.reward_text).strip() or defaults.reward_text,
        daily_limit_per_chat=max(0, min(int(data.get("daily_limit_per_chat") or defaults.daily_limit_per_chat), 50)),
        per_user_daily_limit=max(0, min(int(data.get("per_user_daily_limit") or defaults.per_user_daily_limit), 20)),
        ignore_command_prefixes=_list_or_default(data.get("ignore_command_prefixes"), defaults.ignore_command_prefixes),
        min_message_length=max(1, min(int(data.get("min_message_length") or defaults.min_message_length), 80)),
    )


def _emoji_riddle_item_from_dict(data: Any) -> EmojiRiddleItemConfig | None:
    if not isinstance(data, dict):
        return None
    clue = str(data.get("clue") or "").strip()
    answers = _clean_string_list(data.get("answers"))
    if not clue or not answers:
        return None
    return EmojiRiddleItemConfig(clue=clue, answers=answers, hint=str(data.get("hint") or "").strip())


def _emoji_riddle_config_from_dict(data: Any) -> EmojiRiddleConfig:
    defaults = EmojiRiddleConfig()
    if not isinstance(data, dict):
        return defaults
    items = [
        item
        for raw in data.get("items", [])
        if (item := _emoji_riddle_item_from_dict(raw)) is not None
    ] if isinstance(data.get("items"), list) else defaults.items
    return EmojiRiddleConfig(
        enabled=bool(data.get("enabled", defaults.enabled)),
        command_prefixes=_list_or_default(data.get("command_prefixes"), defaults.command_prefixes),
        duration_seconds=max(5.0, min(float(data.get("duration_seconds") or defaults.duration_seconds), 300.0)),
        reward_text=str(data.get("reward_text") or defaults.reward_text).strip() or defaults.reward_text,
        winner_policy=_winner_policy(data.get("winner_policy"), defaults.winner_policy),
        announce_answer=bool(data.get("announce_answer", defaults.announce_answer)),
        delete_command=bool(data.get("delete_command", defaults.delete_command)),
        items=items or defaults.items,
    )


def _garbled_decode_config_from_dict(data: Any) -> GarbledDecodeConfig:
    defaults = GarbledDecodeConfig()
    if not isinstance(data, dict):
        return defaults
    return GarbledDecodeConfig(
        enabled=bool(data.get("enabled", defaults.enabled)),
        command_prefixes=_list_or_default(data.get("command_prefixes"), defaults.command_prefixes),
        duration_seconds=max(5.0, min(float(data.get("duration_seconds") or defaults.duration_seconds), 300.0)),
        reward_text=str(data.get("reward_text") or defaults.reward_text).strip() or defaults.reward_text,
        winner_policy=_winner_policy(data.get("winner_policy"), defaults.winner_policy),
        announce_answer=bool(data.get("announce_answer", defaults.announce_answer)),
        delete_command=bool(data.get("delete_command", defaults.delete_command)),
        phrases=_list_or_default(data.get("phrases"), defaults.phrases),
    )


def _chat_games_config_from_dict(data: Any) -> ChatGamesConfig:
    defaults = ChatGamesConfig()
    if not isinstance(data, dict):
        return defaults
    return ChatGamesConfig(
        lucky_word=_lucky_word_config_from_dict(data.get("lucky_word")),
        emoji_riddle=_emoji_riddle_config_from_dict(data.get("emoji_riddle")),
        garbled_decode=_garbled_decode_config_from_dict(data.get("garbled_decode")),
        state_store=str(data.get("state_store") or defaults.state_store),
        output_dir=str(data.get("output_dir") or defaults.output_dir),
    )


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _winner_policy(value: Any, default: str) -> str:
    text = str(value or default).strip().lower()
    return text if text in {"first", "random"} else default










def _speech_segments_or_default(value: Any, default: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(default)
    return [str(item).strip() for item in value if str(item).strip()]


def _float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _clamped_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(minimum, min(parsed, maximum))


def _non_negative(value: Any, default: float) -> float:
    """解析非负秒数；缺失/非法回落默认值，允许 0（表示不自动删除）。"""
    if value is None:
        return float(default)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return float(default)


def _list_or_default(value: Any, default: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(default)
    cleaned = [str(item).strip() for item in value if str(item).strip()]
    return cleaned or list(default)


def _mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}***{value[-3:]}"
