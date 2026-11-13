# TelegramBot

Python + Telethon 写的模块化 Telegram 机器人，自带 Web 管理后台。一条 `docker run` 或一个 Portainer 堆栈就能跑在 NAS / 服务器上。

- 功能模块**全部默认关闭**，按需启用，互不干扰
- 配置、会话、数据都在宿主卷里，镜像不含任何凭据
- 后台地址 `http://服务器IP:8088`

## 快速开始

```bash
docker pull shiningth/tgbot:latest
```

Portainer → Stacks → Add stack → 粘贴 [`portainer-stack.yml`](portainer-stack.yml)，设置 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 后部署。

| 项 | 说明 |
| --- | --- |
| 后台 | 端口 `8088`，账号密码由堆栈环境变量决定 |
| 数据目录 | `TGBOT_DATA_DIR`，默认 `/opt/tgbot`；**已有部署必须填原路径** |
| 镜像 | 只有 `latest`（Linux amd64），始终指向最新构建 |

## 功能模块

| 模块 | 用途 |
| --- | --- |
| 红包与口令 | 模板识别、自动领取、口令与算术红包 |
| AI 对话 / 生图 / 解密 | 触发词、上下文、模型服务可配，兼容 OpenAI 与 Anthropic |
| 群友画像 | 按群聊历史生成带原消息引用的画像 |
| 自动签到 | Cron 定时给指定 bot 发命令；网络抖动自动补签，失败/补发成功可私聊通知 |
| 站点签到（网页） | 用 Cookie 直连 PT 站签到页；站点在面板增删，不用重新打包 |
| 磁力数据导入（momo 专用） | 群内回复 CSV + `/导入`，上传前按哈希向 momo 预检排重 |
| 频道自动采集 | 独立个人会话读频道 ed2k 帖，解析番号与封面后写入 momo |
| 消息转发 / 媒体工具 / 小游戏 / 归档 | 转发复制、GIF、词云、贴纸、幸运词、SQLite 文本归档 |
| 管理后台 | 模块开关、配置、运行状态、日志、AI 用量 |

## 三条重点链路

### 一、频道自动采集（ed2k → momo）

机器人不能自己加入别人的频道，这条链路用**独立的个人会话**读取，和签到会话、群机器人互不影响。

1. 登录采集会话（一次即可，会话文件在 `sessions/`，重建容器不丢）：
   ```bash
   docker exec -it tgbot python /app/scripts/telegram_user_login.py
   ```
2. 后台「磁力数据导入(momo 专用) → 频道自动采集」填监控频道——用户名、`@用户名`、`t.me` 链接、数字 ID 都行；**频道主取消公开用户名后只能用数字 ID**，面板会直接提示。
3. 按需开「试运行」先看解析结果，确认无误再关掉；汇总通知可指定「接收 bot 的 Token + chat id」，由那个 bot 直接推给你。

贴两个常被问到的点：

- **元数据**：封面与演员默认 DMM（官方图 800×538，`mono/dvd` 自动解析带厂商前缀的 cid）+ AVbase 演员，各约 1–3 秒；仍缺才继续 AVWikiDB → Jav321 → MGStage。JavDB 默认不启用（部分页面要登录），要加就改 `metadata_sources`。
- **排重**：本地消息 ID → 导入前按番号查 momo 比对 **ed2k 32 位文件哈希**（同片不同文件名也能认）→ 写库时靠磁力哈希兜底。

### 二、群内 CSV 导入

上传前逐行按 `magnets` 重算 32 位文件哈希写回 `magnet_hash`，再用「番号 + 哈希」向 momo 预检；库里已有的行整行摘掉并计入回复里的 `{existing}`，剩下交给 momo 唯一键兜底。预检失败只记日志、不拦导入。

抓不到封面不会自动重试——momo 导入接口没有按行更新能力，重复提交只会新增重复行。事后补图：

```bash
python3 scripts/repair_missing_covers.py --dry-run   # 先看会改哪些
python3 scripts/repair_missing_covers.py --limit 50  # 默认每 10 秒一条
```

### 三、站点签到（PT 站 / 论坛）

NexusPHP 这类站点的签到是一次网页请求，没有官方机器人。这里直接替你去请求签到页：

1. 后台「自动签到 → 站点签到」勾启用，点「新增站点」；
2. 填名称、签到页地址（如 `https://站点/attendance.php`）、定时（默认 `10 8 * * *`）、方式（一般留「自动识别」）；
3. Cookie：浏览器登录后 F12 → Network → 刷新 → 复制请求头的整条 `Cookie` 粘进来。保存后只显示「已保存」，换 Cookie 重新粘即可；
4. 点「立即签到」当场验证，结果写进下方「签到日志」（全局日志保留 30 天）。

支持普通 GET、POST 表单（自动识别字段）、Cloudflare（配 FlareSolverr 代取）、以及**滑动/拖动验证**——页面引用的 `/slide_check_*.js` 里藏着 token 地址，脚本带 `X-Requested-With` 请求一次即可拿到放行 Cookie，不需要无头浏览器。

Cookie 过期时状态会显示「登录失效」，重新粘一条即可。

## 首次配置

1. 用上面的镜像 + `portainer-stack.yml` 部署，设置后台账号密码；
2. 打开 `http://服务器IP:8088` 登录；
3. 申请 `api_id` / `api_hash`、建 bot 拿 Token（见 [Telegram API 与登录](docs/telegram-api.md)）；
4. 填监听群和需要的模块，最后启用 Telegram；个人账号登录在容器 Console 里做；
5. AI 功能另外配置模型服务地址、模型名与 API Key。

## 数据目录与凭据

| 路径 | 内容 |
| --- | --- |
| `config/config.json` | 实际配置（凭据都在这里，注意权限） |
| `sessions/` | Telegram 登录会话，等同于账号访问权，泄露即等于交出账号 |
| `runtime/` | 数据库、状态、日志、生成文件；日志单文件 5 MB × 5 份，另有 30 天清理 |

常用环境变量：`TGBOT_ADMIN_HOST`、`TGBOT_ADMIN_PORT`、`TGBOT_ADMIN_USERNAME`、`TGBOT_ADMIN_PASSWORD`、`TGBOT_DATA_DIR`、`TGBOT_TELEGRAM_API_ID`、`TGBOT_TELEGRAM_API_HASH`、`TGBOT_TELEGRAM_BOT_TOKEN`。

聊天归档默认只存监听范围内的新消息；群友画像与 AI 功能会把处理所需内容发到你自己配置的模型服务。公开镜像与源码包不含凭据、会话、聊天数据库和运行日志。

## 文档

- [部署说明](docs/deployment.md)：堆栈细节、momo 对接、个人会话与群机器人并存
- [Telegram API 与登录](docs/telegram-api.md)：申请 api_id、Bot Token、个人会话
- [变更记录](docs/buildkit-updates.md)：每个版本改了什么
- [架构](docs/architecture.md)

## 安全提醒

`api_hash`、Bot Token、`sessions/*.session`、`config.json` 都属于账号级凭据：不要提交进 Git、不要贴到群里。面板里的 Bot Token 只显示「已保存」，需要更换时重新粘贴即可。
