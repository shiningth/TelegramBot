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

## 2026.11.15.1

- 站点签到新增 `method="quiz"`：北洋园PT（tjupt.org）这类「给一张影片封面 + 6 个片名选项」的答题式签到。
- 题库 `runtime/data/site_checkin_quiz.json` 存「封面 md5 → 正确片名」；命中就选同名单选按钮并 POST，未命中只换题、绝不猜（答错会被扣魔力），换满 8 次仍没有就记「题库里没有当前封面」。
- 题库来源：同一封面反复出现、选项随机，取多次选项集合的交集反推答案；累计 440 道题 / 48 张封面，45 张已唯一确定。
- `site_checkin.py` 新增 `load_quiz_table / _quiz_options / _quiz_pick / _sign_via_quiz`，`sign_site(..., quiz_table=...)` 新参数，模块自动取状态文件同目录的题库；面板「方式」下拉加「答题（看图选片名）」。
- 测试 +4（题库读取容错、选项解析与挑名、命中提交、未知封面不提交），全套 389 passed。

## 2026.11.12.4

- 签到失败/补发会主动通知（Bot API 直发私聊，走「接收 bot」，不需要用户会话）：失败首次发一条「⚠️ 签到失败：任务 / 目标 / 原因 / 会自动重试」，重试成功后发「✅ 签到已补发成功（此前失败 N 次）」，放弃时发「❌ 签到放弃，需手动补」；同一任务同一类提醒每天只发一次（`state.attempts[key].notified`）。
- 重试退避：`_retry_delay()` 以 `retry_interval_seconds`（默认 300s）为基础，每 3 次放宽一档，最长 30 分钟一次；补签窗口默认从 180 分钟放宽到 720 分钟、`max_attempts` 默认 6→10，网络恢复后只要在窗口内就会补上。
- 新增 `telegram_bot/shared/botapi.py`：`send_bot_message()` / `send_bot_message_async()`，失败只记日志不抛异常。
- 面板「自动签到」新增：允许失败通知开关、通知 Chat ID、通知 Bot Token（留空则沿用磁力导入的通知 bot，留空保存不会冲掉已存 token）、补签窗口。
- `styles.css` 新增通用 `.field-row / .cols-2 / .cols-3 / .wide-field`（此前只作用于 `.magnet-form`，其它面板里两列布局失效、输入框会被挤到 200px）。
- 测试：`tests/test_checkin.py` 新增 3 例（退避梯度、失败→补发通知、放弃通知、空 token 保留），全套 375 passed。

## 2026.11.12.3

- 修「网络抖动就整天漏签」：`due_checkin_cron` 只匹配整分钟，04:00 那一刻发送抛 `ConnectionResetError` 就直接跳过一整天。现在 `_tick` 逐任务 try/except，失败记入 `state.attempts`，在 `catchup_minutes` 窗口内按 `retry_interval_seconds` 重试，达到 `max_attempts` 才放弃；新增 `due_checkin_cron_recent()` 往回看窗口内的时刻。
- 单任务崩溃不再连累其它任务（一个 sender 抛异常不影响同分钟的其它签到）。
- 修 `site_checkin.handle_message` 用 `HandlerResult(status="ignored")` 缺 `module_id` 导致每条群消息抛 TypeError（日志里已积累 104 次），改为 `HandlerResult.ignored(self.card.id)`。
- 日志：`telethon.network.*` 压到 ERROR、`telethon.client.updates` 压到 WARNING——11-12 凌晨的重连风暴每小时近 2000 条，两小时就把 5MB×5 的滚动日志刷满，把真正的签到失败挤了出去。
- 测试：`tests/test_checkin.py` 3 例 + `tests/test_site_checkin.py` 1 例，全套 371 passed。

## 2026.11.12.2

- CSV 导入完成提示改成带「校对」段的双段格式（对齐群里另一个导入机器人）：`{total}/{valid}/{dupes}/{invalid}` 来自上传前的必对结果（`csv_prepare.PrepareResult.invalid` 是新增计数：没有可解析磁链的行），`{existing}` 是预检判定「库里已有同番号+同哈希」的条数，`{imported}/{skipped}/{updated}/{errors}` 仍来自 momo 的统计；`{uploaded}` 是实际提交给 momo 的行数（原 `{total}` 的语义被文件总行数接管）。
- `magnet_import._stat_values` 把这些键统一注入模板，缺键时 `render_reply` 原样保留占位符（不会抛异常）。
- 默认模板同步改为新格式；生产配置已通过 PUT 更新。
- 测试：新增 `test_default_reply_reports_file_and_upload_stats`，全套 367 passed。

## 2026.11.12.1

- 新增「站点签到（网页）」模块 `site_checkin`：直接向 PT 站签到页发请求，站点全部来自配置（面板新增/删除即时生效，加站点不用重新打包）。通用签到器 `telegram_bot/shared/sitecheckin.py` 支持三种方式（自动识别 / GET / POST）、Cookie 粘贴、CF 拦截走 FlareSolverr、滑动验证自动过（取 `/slide_check_*.js` 里的 `set_access_token-…` 地址带 `X-Requested-With` 请求一次拿 token cookie）。
- 面板：自动签到任务卡改三列紧凑布局（页高 2086→1756px）；新增站点卡（名称/定时/方式/签到页地址/Cookie，高级选项收纳 sign_url、成功与已签关键字、备注）、「立即签到」按钮、签到日志列表与保留天数。
- 全局日志：签到结果写一条到「日志」页（`站点签到 <站点> → 签到成功（1391ms）：…`）；`LogConfig.keep_days=30` 新增，`PruningRotatingFileHandler` 在滚动时按天删旧文件，启动时也清一次，日志页显示「旧日志保留 30 天 · 最多 5 个文件」。
- 样式：输入控件边框/圆角/内边距/字重提到全局基础规则（站点卡以前是浏览器默认 `2px inset` 深边框），数值框与所有输入框统一浅蓝底（`#eef3ff`，聚焦 `#e4eeff`）；LTX 勾选项文案缩短为「抓取封面与演员」。
- 修复：`styles.css` 里 13 行被 PowerShell 写成乱码的中文注释逐行恢复。
- 测试：新增 `tests/test_site_checkin.py`（10 例）与 `tests/test_logging_setup.py`（3 例），全套 366 passed。

## 2026.11.9.7

- 用户指出 ABF-165 在 DMM 上有（`/mono/dvd/-/detail/=/cid=118abf165/`）→ DMM 直拼路径只覆盖 `digital/video/{code}`，而 mono/dvd 类作品的 cid 带厂商前缀（`118abf165`、`1dldss578`），之前会被判成「DMM 没有」。
- `fetch_dmm_cover` 增加兜底：直拼失败后用 `https://www.dmm.co.jp/search/=/searchstr=<番号去横线>/`（带 `age_check_done=1` cookie，1–4 秒、约 84KB）取真实 cid，再用 `_dmm_cid_matches()` 筛出与本番号对应的那个（容忍 `118` / `1` 之类前缀），然后按 `digital/video/{cid}`、`mono/movie/adult/{cid}` 两个路径验证可访问性（≥60KB）。
- 实测（NAS）：ABF-165 → `mono/movie/adult/118abf165/118abf165pl.jpg`（5.7 秒）；DLDSS-578 → `mono/movie/adult/1dldss578/1dldss578pl.jpg`（4.4 秒，此前被判无图，靠 javbus 补过一张）；IPX-879 0.9 秒、SSIS-858 1.0 秒、SNIS-539 0.7 秒（常规 digital 路径，未受搜索兜底影响）。
- 测试：新增 `test_dmm_cover_resolves_mono_cid_via_search`；`tests/test_ltx_watch.py` **47 passed**。

## 2026.11.9.6

- 元数据源按用户 2026-11-10 指定的顺序重写：**DMM → AVbase → AVWikiDB → Jav321 → MGStage → JavDB**，javbus 整段删除（图片有防盗链，取回来的封面在 momo 里显示不出来）。
- `telegram_bot/shared/javdb.py`：新增 `_META_ORDER` / `_FAST_SOURCES` / `_SOURCE_TIMEOUTS` / `_SOURCE_FUNCS` 与六个源函数 `_fetch_dmm`、`_fetch_avbase`、`_fetch_avwikidb`、`_fetch_jav321`、`_fetch_mgstage`、`_fetch_javdb`；`fetch_meta` 先跑直连的 DMM 与 Jav321（约 1 秒），封面与演员都拿到就停，仍缺才进 Cloudflare 慢源；每源有独立超时上限（dmm 12s / avbase 12s / avwikidb 15s / jav321 10s / mgstage 10s / javdb 40s）。
- JavDB 政策变化（部分页面要登录）：默认源列表里**不含 javdb**，需要时把 `"javdb"` 写进 `metadata_sources`。`config.py` 的 `LtxWatchConfig.metadata_sources` 默认 `["dmm","avbase","avwikidb","jav321","mgstage"]`，并支持字符串解析。
- 实测（NAS 直连 + FlareSolverr）：DMM 0.6–3.7 秒稳定出封面；Jav321 0.6–0.9 秒出演员；AVbase 经 FS 6–33 秒但常无演员数据（`出演者登録なし`）；AVWikiDB 经 FS 15–50 秒，慢但能给出演员；MGStage 一律跳成人认证页，取不到内容；JavDB 经 FS 5–25 秒。
- 单条耗时：能靠 DMM+Jav321 收齐的 1–9 秒（SNIS-539 1.1 秒、IPX-879 8.7 秒）；必须等 AVWikiDB 的约 50 秒；什么都查不到的约 40 秒后放弃并记「缺封面」。
- 面板勾选项与说明文字改为真实顺序，并注明 JavDB 默认不启用。
- 测试：`tests/test_ltx_watch.py` 重写元数据用例（顺序常量、快速源优先、缺字段才落到慢源、Jav321 解析、AVWikiDB 解析），全套 **348 passed**。

- 更正上一版的理解：用户 2026-11-10 说的是「不用补（历史数据）了」，**不是**「新条目也别抓演员」。9.4 把 javbus 演员抓取整段删掉是过度执行。
- `fetch_meta` 恢复三段顺序：① DMM 封面（约 0.7 秒）② javbus 演员（约 1 秒，顺带补封面）③ 只有封面还缺时才查 JavDB。历史 2400+ 行仍不批量回补，`scripts/backfill_authors.py` 只留仓库备用。
- 面板「最近解析」样本恢复显示演员（有就显示，没有就留空，不再出现「演员待补」标记）；勾选项改为「抓取封面与演员（DMM → javbus，缺图再查 JavDB）」，说明文字同步为真实顺序。
- 测试：`test_fetch_meta_falls_back_to_javbus_when_javdb_403`（JavDB 403 → javbus 出演员+封面）、`test_fetch_meta_uses_dmm_cover_and_javbus_author`（DMM 封面 + javbus 演员，source=dmm）。
- 实测（容器内 `check_meta.py`）：单条元数据耗时从 60–75 秒回到约 1–2 秒。

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
Backup directory: /home/user/telegrambot/backups/buildkit-20261028
The stable alias denotes the last version verified during this migration; it is not updated automatically by the build script.
