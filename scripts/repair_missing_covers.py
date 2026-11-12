#!/usr/bin/env python3
"""补齐 momo 里缺封面的 ltx 记录（在 NAS 宿主上运行）。

用法：
    python3 scripts/repair_missing_covers.py                        # 默认只走 DMM / Jav321
    python3 scripts/repair_missing_covers.py --limit 20 --delay 10  # 只修 20 条，每条间隔 10 秒
    python3 scripts/repair_missing_covers.py --dry-run              # 只看会修哪些，不写库
    python3 scripts/repair_missing_covers.py --sources dmm,jav321,avwikidb
    python3 scripts/repair_missing_covers.py --allow-javdb          # 允许按番号搜 JavDB 兜底

说明：
- momo 的导入接口无法按行更新（update_existing 匹配的是内部 import_identity，
  同 code/同哈希会新建重复行），所以这里直接走 SQL 局部更新，只补空的封面/演员字段。
- 默认源顺序与机器人一致：DMM（搜索页给演员 + pl.jpg 封面，约 1–3 秒）→ Jav321。
- **JavDB 默认不参与**：它现在对匿名访问弹登录/Cloudflare，且批量请求会累及出口 IP；
  确实需要时显式加 --allow-javdb（先按番号搜索，再取详情页）。
- 依赖：容器里的 momo-postgres（用 docker exec 执行 psql），以及本仓库的
  telegram_bot.shared.javdb 模块。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram_bot.shared.javdb import fetch_meta, search_detail_by_code  # noqa: E402

DEFAULT_FS = "http://192.0.2.10:8191"
DEFAULT_SOURCES = "dmm,jav321"


def psql(sql: str, container: str = "momo-postgres", database: str = "sehuatang_db") -> str:
    result = subprocess.run(
        ["docker", "exec", container, "psql", "-U", "postgres", "-d", database, "-A", "-t", "-F", "\x01", "-c", sql],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:300])
    return result.stdout


def missing_rows(container: str, database: str) -> list[tuple[int, str]]:
    sql = (
        "select id, coalesce(code,'') from magnet_links "
        "where source_site='ltx' and (cover_url is null or cover_url='') order by id;"
    )
    rows: list[tuple[int, str]] = []
    for line in psql(sql, container, database).strip().splitlines():
        if "\x01" not in line:
            continue
        rid, code = line.split("\x01", 1)
        rows.append((int(rid), code.strip()))
    return rows


def parse_sources(value: str) -> tuple[str, ...]:
    names = tuple(item.strip().lower() for item in str(value or "").split(",") if item.strip())
    return names or ("dmm", "jav321")


def main() -> int:
    parser = argparse.ArgumentParser(description="补齐 momo 里缺封面的 ltx 记录")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=全部）")
    parser.add_argument("--delay", type=float, default=3.0, help="每条之间的间隔秒数")
    parser.add_argument("--flaresolverr", default=DEFAULT_FS, help="FlareSolverr 地址（只有 avbase/avwikidb/mgstage 用得到）")
    parser.add_argument("--sources", default=DEFAULT_SOURCES, help="取图源顺序，默认 dmm,jav321")
    parser.add_argument("--allow-javdb", action="store_true", help="允许用 JavDB 搜索兜底（默认关闭）")
    parser.add_argument("--container", default="momo-postgres")
    parser.add_argument("--database", default="sehuatang_db")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sources = parse_sources(args.sources)
    if args.allow_javdb and "javdb" not in sources:
        sources = sources + ("javdb",)

    rows = missing_rows(args.container, args.database)
    if args.limit:
        rows = rows[: args.limit]
    print(
        "缺封面 %d 条（源：%s%s）%s"
        % (len(rows), ",".join(sources), "，含 JavDB" if args.allow_javdb else "", "（dry-run）" if args.dry_run else ""),
        flush=True,
    )

    fixed = failed = 0
    for index, (rid, code) in enumerate(rows, 1):
        if not code:
            failed += 1
            print(f"[{index}/{len(rows)}] id={rid} 没有番号，跳过", flush=True)
            continue
        meta = fetch_meta("", code=code, flaresolverr=args.flaresolverr, sources=sources) or {}
        cover = meta.get("cover_url", "")
        if not cover and args.allow_javdb:
            # JavDB 是按番号搜索，最慢也最容易触发登录墙，放到最后且必须显式开启
            meta = search_detail_by_code(code, flaresolverr=args.flaresolverr) or {}
            cover = meta.get("cover_url", "")
        if not cover:
            failed += 1
            print(f"[{index}/{len(rows)}] {code} 没取到", flush=True)
            time.sleep(args.delay)
            continue
        author = meta.get("author", "")
        if not args.dry_run:
            sql = (
                "update magnet_links set cover_url='%s', images='%s'::text, "
                "author=coalesce(nullif(author,''),'%s') where id=%d;"
                % (cover.replace("'", "''"), json.dumps([cover]), author.replace("'", "''"), rid)
            )
            psql(sql, args.container, args.database)
        fixed += 1
        print(f"[{index}/{len(rows)}] {code} ✓ {cover[-30:]} author={author or '-'}", flush=True)
        time.sleep(args.delay)

    print(f"完成：补上 {fixed}，失败 {failed}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
