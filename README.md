# TelegramBot

Python + Telethon 写的模块化 Telegram 机器人，自带 Web 管理后台。一条 `docker run` 或一个 Portainer 堆栈就能跑在 NAS / 服务器上。

- 功能模块**全部默认关闭**，按需启用，互不干扰
- 配置、会话、数据都在宿主卷里，镜像不含任何凭据
- 后台地址 `http://服务器IP:8088`

## 快速开始

```bash
docker pull shiningth/tgbot:latest
```

Portainer → Stacks → Add stack → Web editor，粘贴下面的模板，设置 `ADMIN_USERNAME` / `ADMIN_PASSWORD`（可选 `TGBOT_DATA_DIR`、`ADMIN_PORT`、`ADMIN_BIND_IP`）后部署：

```yaml
services:
  telegrambot:
    image: shiningth/tgbot:latest
    container_name: tgbot
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

模板文件也在仓库里：[`portainer-stack.yml`](portainer-stack.yml)（本地自行构建镜像时用那一份，多了 `pull_policy: never`）。

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
| 站点签到（网页） | PT 站/论坛的网页签到：Cookie 直连，自动过滑动验证与云表单，结果带魔力值与连续天数 |
| 磁力数据导入（momo 专用） | 群内回复 CSV + `/导入`，上传前按哈希向 momo 预检排重 |
| 频道自动采集 | 独立个人会话读频道 ed2k 帖，解析番号与封面后写入 momo |
| 消息转发 / 媒体工具 / 小游戏 / 归档 | 转发复制、GIF、词云、贴纸、幸运词、SQLite 文本归档 |
| 管理后台 | 模块开关、配置、运行状态、日志、AI 用量；Telegram 凭证与个人会话都能在网页里填/登录/自检 |

## 两条重点链路

### 一、频道自动采集（ed2k → momo）

机器人不能自己加入别人的频道，这条链路用**独立的个人会话**读取，和签到会话、群机器人互不影响。

1. 登录采集会话（一次即可，会话文件在 `sessions/`，重建容器不丢）：直接在后台「系统设置 → 用户会话」填手机号 + 会话名 `ltx_user` 登录即可；没网页面板可用时再用脚本：
   ```bash
   docker exec -it tgbot python /app/scripts/telegram_user_login.py
   ```
   **收不到验证码**：默认走的是「发到你已登录的 Telegram」，不是短信——去聊天列表最上方的「Login code」或官方账号「Telegram」（id 777000）里找；同一条码只认最后一次请求，重复运行会让旧码失效。还没有就加 `--sms` 强制短信，或加 `--qr` 用已登录的手机扫码（不需要验证码，最稳）。
2. 后台「磁力数据导入(momo 专用) → 频道自动采集」填监控频道——用户名、`@用户名`、`t.me` 链接、数字 ID 都行；**频道主取消公开用户名后只能用数字 ID**，面板会直接提示。
3. 按需开「试运行」先看解析结果，确认无误再关掉；汇总通知可指定「接收 bot 的 Token + chat id」，由那个 bot 直接推给你。

贴两个常被问到的点：

- **元数据**：封面与演员默认 DMM（官方图 800×538，`mono/dvd` 自动解析带厂商前缀的 cid）+ AVbase 演员，各约 1–3 秒；仍缺才继续 AVWikiDB → Jav321 → MGStage。JavDB 默认不启用（部分页面要登录），要加就改 `metadata_sources`。
- **排重**：本地消息 ID → 导入前按番号查 momo 比对 **ed2k 32 位文件哈希**（同片不同文件名也能认）→ 写库时靠磁力哈希兜底。

### 二、群内 CSV 导入

上传前逐行按 `magnets` 算出磁链指纹，只用它做「番号 + 指纹」本地预检；**不改写 `magnet_hash`**（CSV 里是什么就存什么，空值才补）。库里已有的行整行摘掉并计入回复里的 `{existing}`，剩下交给 momo 唯一键兜底。预检失败只记日志、不拦导入。

> **从 2026.11.24.2 之前的镜像升级**：旧版会把 `magnet_hash` 覆盖成 BT infohash，撞上 momo 的 `uq_magnet_hash_source_site`，同一部片进不了第二个板块（CSV 导入「跳过」偏多）。新镜像不再写坏；已经写坏的旧数据用下面这条命令修（幂等，跑完再跑就是「待修 0 行」，会先备份再改）：
>
> ```bash
> # 在跑 momo 的那台机器上，一条命令（先预览，确认后加 --apply 真改）
> docker exec -i momo-postgres sh -c 'cat > /tmp/fix.sh' < scripts/fix_magnet_hash.sh   # 或直接把脚本拷过去
> bash fix_magnet_hash.sh            # 只数：待修多少行
> bash fix_magnet_hash.sh --apply    # 改，并把备份取回到当前目录 magnet_hash_backup.csv
> ```
>
> 不方便取脚本就等价于这条 SQL（容器名/库名按自己的改）：
>
> ```bash
> docker exec -i momo-postgres psql -U postgres -d sehuatang_db -c "update magnet_links m set magnet_hash = split_part(split_part(m.url,'/resources/',2),'?',1) where m.url like '%/resources/%' and m.magnet_hash ~ '^[0-9a-f]{40}\$' and not exists (select 1 from magnet_links x where x.source_site=m.source_site and x.magnet_hash=split_part(split_part(m.url,'/resources/',2),'?',1) and x.id<>m.id)"
> ```
>
> 想让它「每次部署自动跑一次」，把 `scripts/fix-magnet-hash.compose.yml` 里的服务加进堆栈（只需填 `MOMO_DB_PASSWORD`）。

抓不到封面不会自动重试——momo 导入接口没有按行更新能力，重复提交只会新增重复行。事后补图：

```bash
python3 scripts/repair_missing_covers.py --dry-run   # 先看会改哪些
python3 scripts/repair_missing_covers.py --limit 50  # 默认每 10 秒一条
```

### 三、站点签到（PT 站 / 论坛网页）

面板「功能模块 → 自动签到 → 站点签到（网页）」加站点：名称、签到页地址、Cookie、定时（默认每天 08:10/13:10 之类，写法同 cron）。签到器会自己识别页面类型：

- **已登录 Cookie**：浏览器登录站点后复制整条 Cookie 粘进去，面板只显示「已保存」，不回显明文；
- **滑动验证 / 普通表单**：自动取 `slide_check*.js` 里的 token 走一次 XHR 拿到访问令牌，再按页面表单 POST；
- **Cloudflare 托管挑战**：配一个 FlareSolverr 地址（面板里填 `http://IP:8191`）就能代取页面；
- **Turnstile 等强制人机验证**：识别得到但过不去，会明确报「需要人机验证」而不是假装签到成功。

结果写进「签到日志」（保留 30 天，可在面板改）+ 全局日志，并带本次获得的魔力值与连续天数；Cookie 过期会显示「登录失效」。

## 首次配置

1. 用上面的堆栈模板部署，设置后台账号密码；
2. 打开 `http://服务器IP:8088` 登录；
3. 申请 `api_id` / `api_hash`、建 bot 拿 Token（见 [Telegram API 与登录](docs/telegram-api.md)）；
4. 「系统设置 → Telegram 凭证」把这三项填进去，点 **测试凭证** 自检（bot token 用 `getMe` 验、api_id/api_hash 用 `help.getConfig` 验，只探测不登录、不发消息）；保存后自动重连，不用重启容器；
5. 「系统设置 → 用户会话」登个人账号：填手机号 + 会话名（采集用 `ltx_user`，签到用 `user_session`）→ 发验证码 → 填码（开了两步验证再填密码）。验证码发到已登录客户端的「Login code」，不是短信；也可以用它登出重登；
6. 填监听群和需要的模块，最后启用 Telegram；
7. AI 功能另外配置模型服务地址、模型名与 API Key。

同一个 `.session` 不能被两个进程同时打开：登录采集会话前，先在「频道自动采集」里把开关关掉，登录完再打开，否则会报 `database is locked`。

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
