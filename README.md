# TelegramBot

基于 Python 与 Telethon 的模块化 Telegram 机器人，提供由后端直接托管的 Web 管理后台。适合在个人电脑、Linux 服务器或 NAS 上部署。

## Docker Hub / Portainer

镜像：[`shiningth/tgbot`](https://hub.docker.com/r/shiningth/tgbot)。版本 `2026.10.31.1`，也可使用 `latest`；本次提供 Linux amd64 镜像。

```bash
docker pull shiningth/tgbot:2026.10.31.1
```

在 Portainer → Stacks → Add stack 中粘贴本项目的 `portainer-stack.yml`，设置 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 后部署。默认管理端口为 8088，Telegram 和功能模块默认关闭。`TGBOT_DATA_DIR` 默认 `/opt/tgbot`；已有部署必须填写原数据目录。

配置、登录会话和运行数据保存在 `TGBOT_DATA_DIR` 指定的宿主机目录中，镜像不包含实际账号、密码、群 ID、会话或日志。Telegram 凭据的申请及个人账号登录见 [Telegram API 获取步骤](docs/telegram-api.md)，完整部署见 [部署说明](docs/deployment.md)。

## 功能

| 模块 | 说明 |
| --- | --- |
| 红包与口令 | 模板识别、自动领取、口令红包及趣味算术红包 |
| AI 对话 | 可配置触发条件、上下文和模型服务，支持 OpenAI 兼容与 Anthropic 接口 |
| AI 生图 | 文本或参考图片生成、尺寸与发送策略配置 |
| AI 解密 | 识别文本或图片题目，按模板调用 AI 作答 |
| 群友画像 | 根据群聊历史生成带原消息引用的娱乐性画像 |
| 自动签到 | Cron 定时发送、任务启停与执行状态 |
| 磁力数据导入(momo专用) | 群内回复 CSV 后发送 `/导入` 或 `/import`；触发指令与回复文案可在后台自定义，支持授权用户、按文件哈希去重、导入进度与结果统计 |
| 频道自动采集 | 用独立用户会话读取频道（用户名或数字 ID），解析 ed2k 与番号后写入 momo 外部资源分类；支持试运行、FC2 开关与定时汇总通知 |
| 消息转发 | 回复消息后转发、复制文本、引用或生成消息链接 |
| 媒体工具 | GIF 截取转换、群聊词云、语录贴纸 |
| 聊天小游戏 | 幸运词、Emoji 图谜和乱码解码 |
| 聊天归档 | SQLite 文本归档、消息编辑同步与可选历史回填 |
| 管理后台 | 服务启停、模块配置、运行状态、日志和 AI 用量 |

所有功能模块默认关闭，配置后按需启用。部分指令要求由当前登录账号发送；具体触发词及参数可在后台查看和修改。

## 首次配置

1. 使用上方 Docker Hub 镜像和 `portainer-stack.yml` 部署，在堆栈环境变量中设置后台账号和密码。
2. 打开 `http://服务器IP:8088`，使用设置的账号登录。
3. 按 [Telegram API 获取步骤](docs/telegram-api.md) 获取自己的 `api_id`、`api_hash`，并按需填写机器人 Token。
4. 配置监听群和需要的功能模块，最后启用 Telegram；个人账号登录在容器 Console 中完成。
5. AI 功能需另行配置模型服务地址、模型名与 API Key。

镜像为单层自包含构建，Python 运行时、FFmpeg、Tesseract OCR 与中文字体全部打包在同一镜像内，不依赖外部基础镜像，无需在宿主机安装这些依赖。完整配置、持久化与登录操作见 [Docker / Portainer 部署说明](docs/deployment.md)。

## 配置与数据

- `config.example.json`：无凭据的默认配置参考；实际配置使用 `config.json`。
- `sessions/`：Telegram 登录会话，具有账号访问权限，应严格保密。
- `runtime/`：数据库、状态、日志和生成文件；日志单文件 5 MB，最多保留 5 份。
- 支持 `TGBOT_ADMIN_HOST`、`TGBOT_ADMIN_PORT`、`TGBOT_ADMIN_USERNAME`、`TGBOT_ADMIN_PASSWORD`、`TGBOT_TELEGRAM_API_ID`、`TGBOT_TELEGRAM_API_HASH`、`TGBOT_TELEGRAM_BOT_TOKEN` 等环境变量。

聊天归档默认保存监听范围内的新消息文字；历史回填默认关闭。群友画像及 AI 功能会将处理所需内容发送至配置的模型服务。语录贴纸会将消息和头像交给配置的 quote-api 服务，可改用自建地址。请按自己的群聊和数据管理要求决定启用范围。

镜像与公开源码包不包含运行凭据、登录会话、聊天数据库或运行日志。
