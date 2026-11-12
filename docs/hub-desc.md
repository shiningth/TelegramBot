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
| 站点签到（网页） | 用 Cookie 直接请求 PT 站签到页，按 Cron 执行并记录日志；支持自动识别 / GET / POST、滑动验证、Cloudflare 兜底；站点在面板增删，不用重新打包 |
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
- **元数据来源**：封面与演员默认取 DMM（官方图 800×538，mono/dvd 类会自动解析带厂商前缀的 cid）并结合 AVbase 的演员（各约 1–3 秒）；仍缺字段才继续 AVWikiDB → Jav321 → MGStage。JavDB 默认不启用（部分页面要求登录），需要时把 `metadata_sources` 加进去。
- **FlareSolverr 地址**：AVbase、AVWikiDB、JavDB 都在 Cloudflare 后面，填一个 FlareSolverr 地址（如 `http://IP:8191`）才能取到页面；留空则只用直连的 DMM / Jav321。
- **FC2**：默认不入库；开启后 FC2 条目会写入分类，但不抓封面与演员。
- **落库位置**：固定写入 `source_site=ltx`、`forum 名称=LTX破解`（与 CSV 导入共用这两个固定值，面板上已隐藏，避免填错）。
- **汇总通知**：按 cron 定时发送统计（默认每天 21:00）。填了「接收 bot 的 Token」与 chat id 时，由那个 bot 直接推送给指定账号，不会触发对方机器人的命令回复；不填则用采集账号发到「通知目标」。

排重顺序：本地消息 ID → 导入前按番号查询 momo 并比对 **ed2k 32 位文件哈希**（同片不同文件名也能识别）→ 写库时用磁力哈希兜底。补录历史消息时可调大「首次最多回溯条数」，并用 `backfill_stop_after` 在连续 N 条已存在时自动停止。

群内 CSV 导入走同一套判重：上传前逐行按 `magnets` 重算 32 位文件哈希写回 `magnet_hash`（旧库存量里不少是数字占位哈希），再用「番号 + 哈希」向 momo 预检，库里已有的行整行摘掉并计入回复里的 `{existing}`，剩下的才交给 momo 的唯一键兜底。预检失败只记日志，不影响导入。

封面或作者抓不到时不会自动回写：momo 的导入接口没有按行更新能力，重复提交只会新增重复行。面板上的「待补」只是计数，事后可在仓库目录用 `python3 scripts/repair_missing_covers.py` 扫出缺封面的行逐条补齐（默认每 10 秒一条，支持 `--dry-run`、`--limit`）。

## 站点签到（PT 站 / 论坛）

NexusPHP 这类站点的签到是一次网页请求，没有官方机器人可加。这里直接替你去访问签到页，站点全部保存在配置里，**加站点只需在面板点两下，不用重新打包镜像**。

在后台「功能模块 → 自动签到」页面下半部分操作：

1. 勾选「启用站点签到」，点「新增站点」；
2. 填**名称**、**签到页地址**（如 `https://站点/attendance.php`）、**定时**（默认 `10 8 * * *`）、**方式**（一般留「自动识别」）；
3. **Cookie**：浏览器登录后按 F12 → Network → 刷新页面 → 复制请求头里的整条 `Cookie` 粘进来。保存后只显示「已保存」，不会回显明文；要换就重新粘一条。
4. 点「立即签到」当场验证，结果会显示在卡片顶部与下方「签到日志」里。

支持的场景：普通 GET 签到页、需要 POST 表单的签到（自动识别表单字段）、Cloudflare 拦截（配置 FlareSolverr 地址后由它代取）、以及**滑动/拖动验证**——页面引用的 `/slide_check_*.js` 里通常藏着 token 地址，脚本会带上 `X-Requested-With` 请求一次拿到放行 Cookie，不需要无头浏览器。

日志：每次签到都会往「日志」页写一条（`站点签到 <站点> → 签到成功（1635ms）：…`），全局日志默认保留 30 天、最多 5 个文件，滚动时自动清理旧文件。成功/失败可按需推送 Telegram 通知，用与频道采集相同的那套 Bot API Token + chat id 即可。

注意 Cookie 会过期：站点要求重新登录后，旧的 Cookie 会返回登录页，此时签到状态显示「登录失效」，重新粘一条新 Cookie 保存即可。

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
