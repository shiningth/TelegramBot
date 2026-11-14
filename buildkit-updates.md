# TelegramBot image updates

Build directory: /vol3/1000/SSD/github/TelegramBot/TelegramBot-main

Build with a NEW version tag:

    sudo ./build-image.sh 2026.10.29-1

The image includes the application, Python, dependencies, FFmpeg and OCR.
BuildKit exports an image without the legacy parent image chain.
requirements.lock preserves the installed Python dependency versions; update it intentionally when changing dependencies.
The Python base digest is pinned in Dockerfile; update it intentionally for base image upgrades.
The Dockerfile defaults the apt/PyPI sources to mirrors.tuna (deb.debian.org drops connections from this NAS); override with
`--build-arg DEBIAN_MIRROR=https://deb.debian.org --build-arg PIP_INDEX_URL=https://pypi.org/simple` when building elsewhere.

Deployment: Portainer stack telegrambot (239) references `shiningth/tgbot:local`, so no stack yaml change is needed for a
rebuild — recreate the tgbot container after the build (or update the stack image tag in Portainer if you want it pinned).
Keep pull_policy: never on this NAS; the image is built locally and only then pushed to Docker Hub.
Preserve all existing bind mounts and environment settings.
Verify container health, /login and authenticated admin functionality after deployment.

## 2026.11.14.1

- 新增站点：北洋园PT（tjupt.org，NexusPHP）——`attendance.php` 认 `access_token` 这个 JWT cookie，
  单独一条就够；已按 `1 0 * * *` 排程（该站 0/6/7/8/12/18/20/22 点签到前 10 名魔力翻倍）。
- `page_text()` 先做 HTML 实体解码（`&nbsp;` 之类不再原样进日志），再去空白。
- `_snippet()` 改成在所有命中里取最长的一段：原来固定取第一次命中，遇到导航里的
  `[今日已签到]` 就只剩「已签到」两个字，正文那句「已累计签到 806 次…」被丢掉。
## 2026.11.13.2

- 站点签到：`method=browser` 的 TRAWL 调用超时不再判失败——Turnstile 首轮解题常在 80s 以上，
  而我们这边先超时，浏览器里的签到请求其实已经发出去并生效（OurBits 11-14 01:11 就是这样被误报失败）。
  现在超时后补一次快速复核（不解验证、短超时），确认已签就记 `already`，仍不确定记新状态 `pending`（待复核）。
- `_trawl_scrape` 超时预算放宽：服务端 `timeout` 至少 90s、客户端至少 150s（原为 30s / 60s）。
- pt.0ff.cc 这类「把今日签到当补签」的站点：ajax 返回「补签卡不足」不再记失败，改记 `pending`
  （多见于还没到开签时段，下一次调度会重试）。
- 面板与全局日志新增状态「待复核」；单测 +3（超时→pending、超时后复核成功、补签卡→pending），全套 385 passed。

## 2026.11.13.1

- 站点签到新增 `method=browser`：走 TRAWL 原生 `/scrape`（Camoufox 真浏览器 + 内置 Turnstile），页面自身回调提交后
  再取一次判定结果；cookies 必须用数组传，单给 `Cookie` 头 TRAWL 会忽略（实测掉登录态）。
- OurBits 以 `browser` 方式重新加入（cron `10 1/12 * * *`），实测 5.6s 返回「已经签到过了」。
- 站点卡「方式」下拉新增「浏览器（TRAWL）」。
- 单测 +3（两趟 scrape 判定、解不出 Turnstile 报 captcha、TRAWL 不可达报失败），全套 383 passed。
- FlareSolverr 地址同时被 TRAWL 复用（8193 → 容器内 8191）。

## 2026.11.9.4

- 用户 2026-11-10「我让你别补了」→ **彻底停止补演员**：`fetch_meta` 只负责封面（DMM `pl.jpg` 约 1 秒；拿不到才查 JavDB），javbus 不再请求，JavDB 也不再为演员单独调用。JavDB 详情页若在兜封面时顺带带出演员，才顺手记下（零额外请求）。
- `_handle_item` 删掉「封面/演员缺失再搜一次 JavDB」的兜底（60 秒的来源），只保留一次 `fetch_meta`；面板样本行不再显示「演员待补」，勾选项改名「抓取封面（DMM 优先，缺图再查 JavDB）」，说明文字同步。
- 测试：`test_fetch_meta_skips_javbus_when_javdb_403`（403 后不碰 javbus、返回 None）、`test_fetch_meta_uses_dmm_pl_cover_without_javbus_author`、`test_missing_author_does_not_call_javdb_search`。
- 实测：JavDB 经 FlareSolverr 单次 60–75 秒（IPZZ-965 74.4s、SSIS-858 63.4s），DMM 1 秒、javbus 1.1 秒——所以只有封面兜底才允许碰 JavDB。

## 2026.11.9.3

- 元数据抓取顺序改为 **DMM 优先、JavDB 其次、javbus 兜底演员**（用户要求「要补也应该从 DMM → JavDB 这个顺序补」）：
  `fetch_meta` 先按番号拼 DMM `pl.jpg`（800×538，快、不触发 Cloudflare），拿到封面后再去 JavDB 取演员；JavDB 无货才落到 javbus 取演员。抽出内部函数 `_javdb_meta()` 复用原 FlareSolverr/直连+熔断逻辑；只有番号（无 javdb 链接）时走 `search_detail_by_code`。FC2 直接返回 None，不再发请求。
  `_handle_item` 的四级回落简化成两段：先 `fetch_meta`（含 DMM→JavDB→javbus），若封面或演员仍缺，再按番号搜一次 JavDB 补齐。
- 面板措辞把「作者」统一改成「演员」（`author` 就是演员，用户明确指出看不懂）：`app.js` 的「作者待补」→「演员待补」、勾选项「抓取 JavDB 补作者与封面」→「抓取元数据补演员与封面」、通知模板「待补作者 {pending}」→「待补封面 {pending}」（`config.py` 与 `config.example.json` 同步），说明文字改为描述 DMM 优先的顺序。

## 2026.11.9.2

- 入库时补演员兜底：原逻辑只在「没拿到封面」时才按番号搜 JavDB，一旦封面拿到、演员字段缺失（详情页被风控截断或字段为空），入库后 author 就是空的——面板显示「作者待补」（author 就是演员）。
  现在 `_handle_item` 在封面已就绪但 author 为空时，再按番号查一次搜索页只补演员（FC2 跳过，避免无谓请求）。新增用例 `test_missing_author_falls_back_to_search`。
- 另加运维脚本 `scripts/backfill_authors.py`：按番号批量补历史行的 author（只 UPDATE author 一列、带 `coalesce(author,'')=''` 可反复跑，支持 `--only-collected/--workers/--limit/--dry-run`）。用户 2026-11-10 决定不批量补历史（几百行的量没必要，增量有就行），脚本留在仓库备用。

## 2026.11.9.1

- 面板「最近解析」不再拿空字段冒充缺数据：跳过/失败的行不抓元数据，原来一律显示「作者待补 · 缺封面」，看着像被丢弃，
  实际往往是「momo 已有同一个 ed2k」。现在 `_refresh_metrics` 把 `reason` 一起下发，前端按状态渲染——
  `skipped` 显示「已跳过 · <原因>」，`fc2` 显示「FC2 仅展示，不入库」，`failed` 显示「失败 · <原因>」，
  只有真正入库过的行才显示作者/封面徽标。新增用例 `test_samples_carry_skip_reason`。
- 部署标签改为本地：`build-image.sh` 同时打 `:local`（与版本号、`:latest` 同一 ID），Portainer 栈 239 的 compose 改为
  `shiningth/tgbot:local`，重建容器不会因为 `:latest` 指向别处而跑错版本；`:latest` 仍保留，供推 Docker Hub 用。

## 2026.11.8.2

- CSV 预检提速：原来每行都调用 `find_existing_sync`，而它每次请求前都要重新登录 momo，实测约 1 行/秒——2550 行的表要跑 40 分钟，
  面板一直显示「导入中」，用户以为卡死。改为两级优化：
  ① `ExistingLookup` 一次登录、共享 `httpx.Client`，导入前先 `prime()` 用 `source_site=ltx` 分页（`page_size` 接口上限 100）
  把 2180 行整表拉回来建「番号 → 指纹」索引，实测 4.2 秒，之后每行都是本地字典比对（2550 行 ≈ 0 秒）；
  ② 索引不可用时退回 `ThreadPoolExecutor`（默认 8 并发）逐条查询 + 进程内缓存。
- 面板状态可看进度：预检期间 `last.detail` 写成「读取 momo 索引…」/「预检 314/2550」（限流 3 秒一次），不再只有一句「导入中」。
- 预检登录或拉索引失败都是 fail-open：记 warning 后继续导入，交给 momo 唯一索引判重。
- 新增用例：`test_progress_callback_reports_total`、`test_parallel_lookup_drops_existing_rows`、`test_existing_lookup_uses_primed_index`。

## 2026.11.8.1

- `_ED2K` 正则兼容 115 秒传参数：原写法只认 `ed2k://|file|名字|大小|32位哈希|/`，遇到 `|哈希|h=<base32>|/` 会在哈希后截断，
  链接缺收尾斜杠（判为 unsupported）、残余的 `h=…` 还会漏进标题。改为 `\|(?:[^|\r\n]+\|)?/?`，把可选段整段吃掉；标准链接行为不变。
- 新增回归用例 `test_parse_message_keeps_115_secupload_param`。
- 数据修复：16 条受影响记录的 `magnets` 按频道原消息补回 `h=` 段（标题此前已清理），回滚备份 `/tmp/h_rollback_20261108.txt`。

## 2026.11.6.2

- CSV 上传前增加排重预处理（`telegram_bot/shared/csv_prepare.py`）：① 按 `magnets` 里的磁链重算 32 位文件哈希写回 `magnet_hash`
  （旧数据里有 1908 行是数字占位哈希，外部 CSV 也常用占位/空哈希，momo 只按字符串比会放过重复）；② 用「番号 + 哈希」逐个向 momo
  预检（`find_existing_sync`），库里已存在的行整行摘掉并计入 `{existing}`，同一文件内重复只留第一行；预检失败不阻断导入，退回交给 momo 判重。
- 新增回复占位符 `{existing}`（库里已有/已跳过）与 `{normalized}`（哈希被规范化的行数）；全部行都已存在时不再上传空文件，直接按 0 新增回复。

## 2026.11.6.1

- 移除面板「更新已存在的记录」「跳过重复记录」两个勾选框：momo 导入接口没有按行更新能力（`update_existing=true` 会新增重复行），
  也没有「重复也新增」模式，两个开关都会误导。`magnet_import_config_from_dict` 固定 `update_existing=False` / `skip_duplicates=True`，
  面板改为一行说明：重复记录由 momo 按磁链哈希自动跳过。

## 2026.11.5.6

- 监控频道支持数字 ID：频道主取消公开用户名后（`UsernameNotOccupied`）只能按 `-100…` 访问，
  `normalize_channel()` 统一处理用户名 / `@用户名` / `t.me` 链接 / 数字 ID；用户名失效时面板错误栏直接提示改填数字 ID。
- 修复 `cover_url` 未写入导入 CSV（`import_rows_sync` 缺列 + `extrasaction="ignore"` 静默丢弃）导致 momo 列表无封面。
- 删除容器内补丁式的 `_queue_pending` / `_retry_pending`（`update_existing` 会新增重复行，momo 无按行更新能力），
  改为缺图只记 `state["missing_cover"]` + `scripts/repair_missing_covers.py` 事后用 SQL 补。
- JavDB 搜索/详情统一走 FlareSolverr（直连 403），DMM 占位图阈值 15000 → 60000。

## 2026.10.31.2

- 磁力数据导入：手动触发指令（`/导入`、`/import`、`/import@机器人`、`@机器人 导入`）与全部回复文案改为后台可配置，
  默认值与旧行为完全一致；后台表单下方带实时预览。文案随 `config.json` 保存，改文案不需要重新构建镜像。
- Dockerfile 的 apt / PyPI 源默认改为 mirrors.tuna：deb.debian.org 从本机只有约 300 KB/s 且会断流，
  装 ffmpeg/中文字体时整次构建随机失败；海外构建可用 `--build-arg DEBIAN_MIRROR=... PIP_INDEX_URL=...` 覆盖。
- 已推送 Docker Hub：`shiningth/tgbot:2026.10.31.2` 与 `shiningth/tgbot:latest`。

Initial version: telegrambot:2026.10.28-buildkit.1
Rollback version: telegrambot:rollback-20261028
Backup directory: /home/shiningth/telegrambot/backups/buildkit-20261028
The stable alias denotes the last version verified during this migration; it is not updated automatically by the build script.
