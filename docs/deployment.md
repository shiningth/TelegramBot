# Docker / Portainer 部署说明

## Docker + Portainer 堆栈

适用于 Portainer 的 Docker Standalone 环境。直接使用 Docker Hub 镜像，无需本机构建：

```bash
docker pull shiningth/tgbot:latest
```

在 Portainer → Stacks → Add stack → Web editor 中粘贴项目根目录的 `portainer-stack.yml`。在 Environment variables 设置 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD`；设置 `TGBOT_DATA_DIR` 为宿主机上的数据目录（默认 `/opt/tgbot`）；可选 `ADMIN_PORT`（默认 8088）、`ADMIN_BIND_IP`（默认 0.0.0.0）。部署后访问 `http://服务器IP:8088`。镜像只提供 `latest`（不再保留历史版本 tag）：首次部署从 Docker Hub 拉取，更新时重新 `docker pull shiningth/tgbot:latest` 并重建容器即可。

申请自己的 Telegram `api_id`、`api_hash` 和机器人 Token，请按 [Telegram API 获取步骤](telegram-api.md) 操作。需要本机构建时可使用 `docker build -t telegrambot:local .` 并相应修改堆栈镜像名。

首次启动自动生成 `/data/config/config.json`，Telegram 默认关闭。目录结构与堆栈挂载一致：

```text
TGBOT_DATA_DIR/
├── config/
│   └── config.json
├── sessions/
│   └── *.session
└── runtime/
    ├── data/
    ├── logs/
    └── tmp/
```

| 宿主机目录 | 容器路径 | 用途 |
| --- | --- | --- |
| `${TGBOT_DATA_DIR}` | `/data` | 配置和数据根目录 |
| `${TGBOT_DATA_DIR}/sessions` | `/app/sessions` | Telegram 登录会话 |
| `${TGBOT_DATA_DIR}/runtime` | `/app/runtime` | 数据库、状态、日志及临时文件 |

更新时保留 `TGBOT_DATA_DIR`、挂载和以下启动参数，只重新拉取 `latest` 镜像并重建容器：

```yaml
command: ["python", "-m", "telegram_bot", "--config", "/data/config/config.json"]
```

已有 `/data/config/config.json` 时优先使用它。旧版若仅有 `/data/config.json`，新镜像默认启动时会复制到新路径，保留原文件；不会覆盖已有的新路径配置。使用旧命名卷的部署也应保留原卷映射，不要直接换成一个空的宿主机目录。备份时保存整个数据目录；升级前停止服务以保证 SQLite 文件一致。

### 配置 Telegram 和 momo（原 MagnetBoard）

在 Portainer 容器详情中找到实际容器名，下面用 `CONTAINER` 代替。先复制配置到主机编辑：

```bash
docker cp CONTAINER:/data/config/config.json ./config.json
```

在现有 JSON 中修改对应字段，保留其余配置：

```json
{
  "telegram": {
    "enabled": true,
    "api_id": 123456,
    "api_hash": "填写自己的 api_hash",
    "bot_token": "填写自己的机器人 token",
    "session_name": "telegram_bot",
    "session_dir": "sessions",
    "monitored_groups": ["-1001234567890"]
  },
  "modules": {"magnet_import": true},
  "magnet_import": {
    "watch_chats": ["-1001234567890"],
    "sender_ids": ["123456789"],
    "sender_usernames": [],
    "file_name_pattern": "\\.csv$",
    "api_base_url": "http://192.0.2.10:8022",
    "api_username": "admin",
    "api_password": "填写 momo 密码",
    "skip_duplicates": true,
    "update_existing": false
  }
}
```

`monitored_groups` 与 `watch_chats` 均需包含目标群。`sender_ids` 填允许上传者的 Telegram 数字 ID，或使用 `sender_usernames`；两者任选其一匹配即可。**两者都为空时不会导入，不代表允许所有人。** 文件名规则 `\\.csv$` 接收任意 CSV 文件名，默认规则则只接收 `x1080x` 开头的 CSV。

`api_base_url` 填 momo 的可达地址。容器里的 `127.0.0.1` 指 TelegramBot 自身，不能指向另一个 momo 容器。可使用服务器局域网 IP 和 momo 已映射端口；若使用容器服务名，需把两个服务接入同一个 Docker 网络。

保存配置后，在后台停止 Telegram 服务，再复制配置并重启容器：

```bash
docker cp ./config.json CONTAINER:/data/config/config.json
docker restart CONTAINER
```

机器人账号需要加入目标群并能接收文件消息，必要时在 BotFather 调整群隐私设置。若使用个人账号，将 `bot_token` 留空，先保持 `telegram.enabled=false`，在 Portainer 的交互式容器 Console 中执行以下命令完成登录：

```bash
python -c 'from telegram_bot.config import AppConfig; from telethon.sync import TelegramClient; c=AppConfig.load(__import__("pathlib").Path("/data/config/config.json")); client=TelegramClient("/app/sessions/"+c.telegram.session_name,c.telegram.api_id,c.telegram.api_hash); client.start(); client.disconnect()'
```

按提示输入手机号、验证码及两步验证密码，成功后再设置 `telegram.enabled=true` 并重启。不要在同一会话被主服务使用时重复运行登录命令。

### 个人账号自动签到与群机器人同时运行

在 `sessions` 持久化目录放置已登录的个人账号会话，例如 `user_session.session`。在 `checkin` 中配置 `user_session_name: "user_session"`（不带 `.session` 后缀）及 `user_id: "你的数字用户ID"`，签到会独立使用此个人会话，群机器人继续使用 `telegram.session_name`。程序只复用已授权会话，不会在定时任务里要求交互登录；身份不符、机器人会话或会话过期时拒绝发送。

每日北京时间 04:00 向 `@example_checkin_bot` 发送 `/checkin`：任务 `target` 填 `@example_checkin_bot`，`message` 填 `/checkin`，`cron` 填 `0 4 * * *`，启用任务及签到模块。堆栈时区 `TZ=Asia/Shanghai`。签到记录表示发送成功，不代表目标机器人一定已完成签到，需要结合目标回复确认。服务器在计划时间停止时不会补发。

### 导入验证与限制

#### LTX 频道自动采集（ed2k）

后台「磁力数据导入(momo专用)」页的「LTX 频道自动采集」段负责这条链路：用**独立的用户会话** `ltx_user` 读取频道，解析每条消息的 ed2k / 番号 / 大小，按「番号 + ed2k」与 momo 比对后写入 `外部资源 → LTX破解`（`source_site=ltx`，`forum_id=forum_type=forum_name=LTX破解`）。

**获取采集会话**（只需一次，机器人无法自行加入他人频道，必须借用个人账号）：

```bash
docker exec -it tgbot python /app/scripts/telegram_user_login.py
```

按提示输入带国家码的手机号，验证码会出现在 Telegram 里与「Telegram」官方账号的对话中；两步验证账号会再要一次密码。成功后在 `sessions/ltx_user.session` 生成会话文件，重建容器不会丢失。该会话与签到用的 `user_session` 相互独立。

「监控频道」可填用户名、`@用户名`、`t.me` 链接或数字 ID（`-100…`）。频道主取消公开用户名后只能按数字 ID 访问，此时面板错误栏会直接提示改填数字 ID，不必翻日志。

其余行为：`dry_run` 为 `true` 时只解析不写库（首轮会全量回扫，日志与面板显示「试运行命中」）；切换为 `false` 时会自动清空试运行记录并重新扫描。作者与封面优先取自消息里的 JavDB 详情按钮，取不到时按番号搜索；JavDB 直连常被 Cloudflare 403，配好 `metadata_flaresolverr` 后会经 FlareSolverr 访问，仍失败则退回 DMM 封面。抓取失败**不会**自动回写：momo 的导入接口没有按行更新能力，`update_existing` 实测会新增重复行，因此只把番号记进 `missing_cover` 计数，事后用 `python3 scripts/repair_missing_covers.py`（宿主上运行，支持 `--dry-run` / `--limit` / `--delay`）扫出缺封面的行逐条补齐。`notify_enabled` + `notify_cron` 控制定时汇总（默认每天 21:00，目标 `me` 即收藏夹），面板上的「发送测试通知」可立即验证。

#### 手动触发（推荐用于按需导入）

将 `magnet_import.manual_only` 设置为 `true`，并启用 `modules.magnet_import`。在配置的群中，回复一条 CSV 文件消息，发送 `/导入` 或 `/import`，也兼容 `@机器人用户名 导入`（替换为实际用户名）。仅上传文件或只 @机器人不会导入。`/导入` 是中文文本别名，Telegram 不会将它识别为标准 Bot Command；使用它需将机器人设为群管理员或关闭隐私模式。若群内有多个机器人且开启隐私模式，Telegram 可能不会将不带用户名的 `/import` 分发给本机器人；可将本机器人设为群管理员，或在 BotFather 关闭隐私模式后重新入群。

触发指令和全部回复文案都保存在配置里，可在后台「磁力数据导入」页直接修改，保存后新的消息即按新规则处理，无需改代码或重新构建镜像：`manual_commands` 为直接发送的指令（默认 `/import`、`/导入`，精确匹配、大小写不敏感，也支持 `/import@机器人`），`manual_mention_commands` 为 `@机器人 指令` 中的触发词（默认 `导入`、`/import`）。回复文案包括 `reply_text`（自动导入成功）、`manual_success_text`（手动导入成功）以及 `reply_denied_text`、`reply_need_reply_text`、`reply_not_csv_text`、`reply_duplicate_text`、`reply_oversized_text`、`reply_missing_credentials_text`、`reply_failed_text`，支持 `{file}`、`{size}`、`{max_size}`、`{command}`、`{imported}`、`{updated}`、`{skipped}`、`{errors}`、`{total}` 占位符，写错的占位符会原样显示，留空则恢复默认文案。后台表单下方会实时预览每条回复的效果。

手动模式中的 `sender_ids` / `sender_usernames` 限制发指令的人，不限制文件上传者；两者均为空时仅群管理员或群主可触发。`file_name_pattern` 设为 `\\.csv$` 可接收任意名称的 CSV。同一文件消息在处理期间和成功后会阻止重复触发，失败后可再次触发。机器人需要能读取回复的文件消息并发送结果；必要时在 BotFather 关闭隐私模式后重新入群，或将其设为群管理员。

`silent_denied`（默认 `true`）决定非授权用户的指令怎么处理：`true` 时机器人完全不回复，只在后台「拦截 N 次」里计数并写一行日志，群里看不出它被调用过；只有 `sender_ids` / `sender_usernames` 里的账号能触发。设为 `false` 则恢复「仅群管理员或已授权用户可以触发导入」这类可见提示，便于排查为什么别人的指令没反应。注意授权名单非空时，群主和管理员同样不会获得放行。

流程为：允许的群成员上传 CSV → 下载临时文件 → `POST /api/auth/login` 登录 momo → `POST /api/data/import-jobs` 创建资源导入任务 → 轮询任务至完成 → momo 导入数据库 → 群内回复。没有直接挂载或修改 momo 数据库。

上传一份 momo 支持的 CSV，检查后台 `magnet_import` 模块的最近状态、容器日志及 momo 的实际新增记录。资源导入忽略 CSV 的 ID，由 momo 本机分配；`auto_push=false`，仅入库，不自动推送。排重分两道：上传前 `telegram_bot/shared/csv_prepare.py` 先按 `magnets` 重算 32 位文件哈希写回 `magnet_hash`，再用「番号 + 哈希」向 momo 预检、把库里已有的行整行摘掉（回复里的 `{existing}`）；剩下的行由 momo 按 `magnet_hash` 唯一键再跳一次。`update_existing` 固定为 `false`（momo 无按行更新能力，置 `true` 只会新增重复行），面板上对应的勾选框已移除。预检失败（登录不上、接口报错）只记日志、不阻断导入。CSV 列名和格式需符合 momo 资源导入要求；如果新版 momo 改动了上述接口，需要调整 `telegram_bot/shared/magnetboard_api.py`。当前实现不会自动扫描启动前的历史 CSV，也不会自动重试失败的导入请求；修复故障后可重新上传触发。同一消息仅保留最近 200 条处理记录。
