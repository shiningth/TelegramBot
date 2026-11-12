# TelegramBot

基于 Python 与 Telethon 的模块化 Telegram 机器人，提供由后端直接托管的 Web 管理后台。适合在个人电脑、Linux 服务器或 NAS 上部署。

## Docker Hub / Portainer

镜像：[`shiningth/tgbot`](https://hub.docker.com/r/shiningth/tgbot)。只提供 `latest`（始终指向最新构建，不再保留历史版本 tag）；本次提供 Linux amd64 镜像。

```bash
docker pull shiningth/tgbot:latest
```

在 Portainer → Stacks → Add stack 中粘贴本项目的 `portainer-stack.yml`，设置 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 后部署。默认管理端口为 8088，Telegram 和功能模块默认关闭。`TGBOT_DATA_DIR` 默认 `/opt/tgbot`；已有部署必须填写原数据目录。

配置、登录会话和运行数据保存在 `TGBOT_DATA_DIR` 指定的宿主机目录中，镜像不包含实际账号、密码、群 ID、会话或日志。Telegram 凭据的申请及个人账号登录见 [Telegram API 获取步骤](#获取-telegram-api-与登录)，完整部署见 [部署说明](#portainer-堆栈完整示例)。

## 功能

| 模块 | 说明 |
| --- | --- |
| 红包与口令 | 模板识别、自动领取、口令红包及趣味算术红包 |
| AI 对话 | 可配置触发条件、上下文和模型服务，支持 OpenAI 兼容与 Anthropic 接口 |
| AI 生图 | 文本或参考图片生成、尺寸与发送策略配置 |
| AI 解密 | 识别文本或图片题目，按模板调用 AI 作答 |
| 群友画像 | 根据群聊历史生成带原消息引用的娱乐性画像 |
| 自动签到 | Cron 定时发送、任务启停与执行状态 |
| 磁力数据导入(momo专用) | 群内回复 CSV 后发送 `/导入` 或 `/import`；支持授权用户、上传前按磁链哈希向 momo 预检排重、导入进度与结果统计 |
| 频道自动采集 | 用独立用户会话读取频道（用户名或数字 ID），解析 ed2k 与番号后写入 momo 外部资源分类；支持试运行、FC2 开关与定时汇总通知 |
| 消息转发 | 回复消息后转发、复制文本、引用或生成消息链接 |
| 媒体工具 | GIF 截取转换、群聊词云、语录贴纸 |
| 聊天小游戏 | 幸运词、Emoji 图谜和乱码解码 |
| 聊天归档 | SQLite 文本归档、消息编辑同步与可选历史回填 |
| 管理后台 | 服务启停、模块配置、运行状态、日志和 AI 用量 |

所有功能模块默认关闭，配置后按需启用。部分指令要求由当前登录账号发送；具体触发词及参数可在后台查看和修改。

## 频道自动采集（ed2k → momo）

机器人无法自行加入他人频道，因此这条链路使用**独立的个人会话**读取频道；它与签到会话、群机器人的会话互不影响。

登录采集会话（一次即可，会话文件保存在 `sessions/`，重建容器不丢）：

```bash
docker exec -it tgbot python /app/scripts/telegram_user_login.py
```

在后台「磁力数据导入(momo专用) → 频道自动采集」中设置：

- **监控频道**：用户名、`@用户名`、`t.me` 链接或数字 ID（形如 `-1001234567890`）都可以填。频道主取消公开用户名后就只能用数字 ID，此时面板的错误栏会直接提示，不用去翻日志。
- **轮询间隔**：默认 300 秒；频道消息密集时建议不低于 120 秒。
- **试运行**：开启时只解析不写库，面板会显示「最近解析」样本（番号 / 时间 / 内容）。
- **抓取封面与演员**：封面走 DMM 官方图（`pics.dmm.co.jp/.../pl.jpg`，800×538，约 0.7–5 秒；mono/dvd 类作品的 cid 带厂商前缀，如 `118abf165`）；演员由 DMM 搜索页与 AVbase **合并**给出（每轮都查，约 1–2 秒）；仍缺字段时才按顺序再试 AVWikiDB（10–50 秒）→ Jav321 → MGStage → JavDB。历史数据不批量补，只对新入库条目生效。
- **JavDB**：现在匿名访问部分页面会弹登录墙 / Cloudflare，默认不在采集链里；确实要启用就把 `javdb` 写进配置的 `metadata_sources`。已入库的 jdbstatic 封面不受影响——图片走独立 CDN。
- **FlareSolverr 地址**：AVbase、AVWikiDB、JavDB 都被 Cloudflare 拦，填一个地址（如 `http://IP:8191`）才能取到；留空则只走 DMM 与 Jav321（封面与主演员仍能拿到）。
- **FC2**：默认不入库；开启后 FC2 条目会写入分类，但不抓封面与演员。
- **落库位置**：固定写入 `source_site=ltx`、`forum 名称=LTX破解`（与 CSV 导入共用这两个固定值，面板上已隐藏，避免填错）。
- **汇总通知**：按 cron 定时发送统计（默认每天 21:00）。填了「接收 bot 的 Token」与 chat id 时，由那个 bot 直接推送给指定账号，不会触发对方机器人的命令回复；不填则用采集账号发到「通知目标」。

排重顺序：本地消息 ID → 导入前按番号查询 momo 并比对 **ed2k 32 位文件哈希**（同片不同文件名也能识别）→ 写库时用磁力哈希兜底。补录历史消息时可调大「首次最多回溯条数」，并用 `backfill_stop_after` 在连续 N 条已存在时自动停止。

群内 CSV 导入走同一套判重：上传前逐行按 `magnets` 重算 32 位文件哈希写回 `magnet_hash`（旧库存量里不少是数字占位哈希），再用「番号 + 哈希」向 momo 预检——启动时先把 momo 的磁链表按 `source_site` 分页拉成内存索引（两千行约 4 秒，`page_size` 上限 100），之后逐行比对不再发请求；库里已有的行整行摘掉并计入回复里的 `{existing}`，哈希被重算过的行数计入 `{normalized}`，剩下的才交给 momo 的唯一键兜底。面板在预检阶段会显示「读取 momo 索引…」与「预检 done/total」。账号没配或接口不可达时预检整体跳过（只记一条日志），导入照常进行。

封面抓不到时不会自动回写：momo 的导入接口没有按行更新能力，重复提交只会新增重复行。面板「最近解析」会按状态显示原因（入库 / `已跳过 · momo 已存在同一 ed2k` / `FC2 仅展示，不入库` / 失败原因），「待补」只是缺封面的计数，事后可在仓库目录用 `python3 scripts/repair_missing_covers.py` 扫出缺封面的行逐条补齐（默认源 `dmm,jav321`、每 3 秒一条，支持 `--limit`、`--dry-run`；要按番号搜 JavDB 时加 `--allow-javdb`）。

## 首次配置

1. 使用上方 Docker Hub 镜像和 `portainer-stack.yml` 部署，在堆栈环境变量中设置后台账号和密码。
2. 打开 `http://服务器IP:8088`，使用设置的账号登录。
3. 按 [Telegram API 获取步骤](#获取-telegram-api-与登录) 获取自己的 `api_id`、`api_hash`，并按需填写机器人 Token。
4. 配置监听群和需要的功能模块，最后启用 Telegram；个人账号登录在容器 Console 中完成。
5. AI 功能需另行配置模型服务地址、模型名与 API Key。

镜像为单层自包含构建，Python 运行时、FFmpeg、Tesseract OCR 与中文字体全部打包在同一镜像内，不依赖外部基础镜像，无需在宿主机安装这些依赖。完整配置、持久化与登录操作见 [Docker / Portainer 部署说明](#portainer-堆栈完整示例)。

## 配置与数据

- `config.example.json`：无凭据的默认配置参考；实际配置使用 `config.json`。
- `sessions/`：Telegram 登录会话，具有账号访问权限，应严格保密。
- `runtime/`：数据库、状态、日志和生成文件；日志单文件 5 MB，最多保留 5 份。
- 支持 `TGBOT_ADMIN_HOST`、`TGBOT_ADMIN_PORT`、`TGBOT_ADMIN_USERNAME`、`TGBOT_ADMIN_PASSWORD`、`TGBOT_TELEGRAM_API_ID`、`TGBOT_TELEGRAM_API_HASH`、`TGBOT_TELEGRAM_BOT_TOKEN` 等环境变量。

聊天归档默认保存监听范围内的新消息文字；历史回填默认关闭。群友画像及 AI 功能会将处理所需内容发送至配置的模型服务。语录贴纸会将消息和头像交给配置的 quote-api 服务，可改用自建地址。请按自己的群聊和数据管理要求决定启用范围。

镜像与公开源码包不包含运行凭据、登录会话、聊天数据库或运行日志。


---

# 获取 Telegram API 与登录

## 1. 获取 api_id / api_hash

本项目使用 Telethon（MTProto），即使登录机器人，也需要自己的 `api_id` 和 `api_hash`。

1. 先用 Telegram 官方客户端注册并登录自己的账号。
2. 打开 [my.telegram.org](https://my.telegram.org)，输入带国家区号的手机号，例如 `+86…`，按页面提示输入登录验证码。
3. 打开 **API development tools**；已有应用时会直接显示应用信息。
4. 没有应用时，填写 **App title**（应用名称）和 **Short name**（简短名称），按表单要求选择平台并创建应用。
5. 保存页面中的 **App api_id** 和 **App api_hash**，分别填入后台 Telegram 设置或配置文件的 `telegram.api_id`、`telegram.api_hash`。

这是 Telegram 提供的申请流程：[官方说明](https://core.telegram.org/api/obtaining_api_id)。每个手机号目前只能关联一个 API ID；请使用自己的凭据，不要使用他人公开的示例值。

## 2. 获取机器人 Token

1. 打开官方 [@BotFather](https://t.me/BotFather)，发送 `/newbot`。
2. 按提示设置显示名称和机器人用户名，保存返回的 Token。
3. 将 Token 填入 `telegram.bot_token`。**Bot Token 和 api_hash 是不同的凭据，不能互相替代。**
4. 将机器人加入需要监听的群，填写群 ID，最后启用 Telegram。

使用中文 `/导入` 时，需要机器人能收到普通群消息：可设为群管理员，或在 BotFather 发送 `/setprivacy`、选择机器人、点击 **Disable**，然后将机器人移出群再加入。导入授权另由 `magnet_import.sender_ids` 控制。

## 3. 登录个人账号用于自动签到

机器人 Token 只能登录机器人。给自己的账号签到时，需额外登录个人会话；这不会更换群机器人的会话。

在 Portainer 的 TelegramBot 容器 **Console** 中选择 `/bin/bash` 并连接。先配置好 api_id / api_hash，然后执行：

```bash
python -c 'import json; from telethon.sync import TelegramClient; c=json.load(open("/data/config/config.json"))["telegram"]; client=TelegramClient("/app/sessions/checkin_user",c["api_id"],c["api_hash"]); client.start(); me=client.get_me(); print("登录用户 ID:",me.id); client.disconnect()'
```

手机号、验证码和可能的两步验证密码直接在交互式控制台输入。命令会复用已登录的 `checkin_user.session`；若配置文件位于其他路径，请相应替换命令中的路径。

在配置文件的 `checkin` 中设置 `user_session_name` 为 `checkin_user`，`user_id` 为刚显示的个人数字 ID。重启后，签到模块就会使用该个人会话；启用签到模块并配置目标、Cron 与签到内容。不要让其他进程同时使用同一份个人会话文件。

api_hash、Bot Token、个人会话文件均应保密。公开镜像不含这些内容，每位部署者需自行配置。


## 统一数据目录

配置固定为 `/data/config/config.json`；`TGBOT_DATA_DIR` 下保留 `config/`、`sessions/`、`runtime/`。升级时保持原宿主机路径和挂载。旧版根目录 `/data/config.json` 会在新路径不存在时自动复制，原文件保留，新路径已有配置时不会覆盖。

## Portainer 堆栈完整示例

> 若你在本机自行构建镜像（不推送到 registry），可在 `image` 下加 `pull_policy: never`，避免 Compose 尝试从 Docker Hub 拉取同名 tag。从 Hub 拉取镜像的部署方式不要加这一行。

```yaml
# Docker Standalone 环境；设置 ADMIN_USERNAME 和 ADMIN_PASSWORD 后部署。
services:
  telegrambot:
    image: shiningth/tgbot:latest
    container_name: tgbot
    command: ["python", "-m", "telegram_bot", "--config", "/data/config/config.json"]
    restart: unless-stopped
    init: true
    stop_grace_period: 45s
    ports:
      - "${ADMIN_BIND_IP:-0.0.0.0}:${ADMIN_PORT:-8088}:8088"
    environment:
      TZ: Asia/Shanghai
      TGBOT_ADMIN_HOST: 0.0.0.0
      TGBOT_ADMIN_PORT: "8088"
      TGBOT_ADMIN_USERNAME: "${ADMIN_USERNAME:?请设置后台用户名}"
      TGBOT_ADMIN_PASSWORD: "${ADMIN_PASSWORD:?请设置后台密码}"
    volumes:
      - ${TGBOT_DATA_DIR:-/opt/tgbot}:/data
      - ${TGBOT_DATA_DIR:-/opt/tgbot}/sessions:/app/sessions
      - ${TGBOT_DATA_DIR:-/opt/tgbot}/runtime:/app/runtime
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

```
