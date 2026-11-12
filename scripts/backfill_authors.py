#!/usr/bin/env python3
"""按番号补齐 momo 里 author（演员）为空的 ltx 记录（在 NAS 宿主上运行）。

用法：
    python3 scripts/backfill_authors.py                       # 补全部缺 author 的行
    python3 scripts/backfill_authors.py --limit 20 --dry-run  # 只看会补哪些
    python3 scripts/backfill_authors.py --workers 6           # 并发数（默认 4）

说明：
- 只补 author 为空的 ltx 行，只写 author 一列，不动封面/标题/日期，可反复跑。
- 番号本身不含演员，必须查库：JavDB 搜索页（经 FlareSolverr 过 Cloudflare）→ 详情页回落。
- 老数据里 magnet_hash 是数字（sehuatang 导入）的行同样没有 author，本脚本默认一起补；
  想只补采集来的行，加 --only-collected（magnet_hash 为 32 位十六进制）。
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram_bot.shared.javdb import fetch_meta, search_detail_by_code  # noqa: E402

DEFAULT_FS = "http://192.0.2.10:8191"
SEP = "\x01"


def psql(sql: str, container: str = "momo-postgres", database: str = "sehuatang_db") -> str:
    import subprocess

    result = subprocess.run(
        ["docker", "exec", container, "psql", "-U", "postgres", "-d", database, "-A", "-t", "-F", SEP, "-c", sql],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:300])
    return result.stdout


def missing_rows(container: str, database: str, *, only_collected: bool) -> list[tuple[int, str]]:
    cond = "coalesce(author,'')='' and coalesce(code,'')<>''"
    if only_collected:
        cond += " and magnet_hash ~ '^[0-9a-f]{32}$'"
    sql = f"select id, coalesce(code,'') from magnet_links where source_site='ltx' and {cond} order by id;"
    rows: list[tuple[int, str]] = []
    for line in psql(sql, container, database).strip().splitlines():
        if SEP not in line:
            continue
        rid, code = line.split(SEP, 1)
        rows.append((int(rid), code.strip()))
    return rows


def lookup(code: str, flaresolverr: str) -> str:
    try:
        meta = search_detail_by_code(code, flaresolverr=flaresolverr, timeout=40) or {}
    except Exception:  # noqa: BLE001
        meta = {}
    author = (meta.get("author") or "").strip()
    if author:
        return author
    try:
        meta = fetch_meta("", code=code, flaresolverr=flaresolverr) or {}
    except Exception:  # noqa: BLE001
        meta = {}
    return (meta.get("author") or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="补齐 ltx 记录的 author（演员）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=全部）")
    parser.add_argument("--workers", type=int, default=4, help="并发查询数（默认 4）")
    parser.add_argument("--flaresolverr", default=DEFAULT_FS, help="FlareSolverr 地址，留空则直连 JavDB")
    parser.add_argument("--container", default="momo-postgres")
    parser.add_argument("--database", default="sehuatang_db")
    parser.add_argument("--only-collected", action="store_true", help="只补 magnet_hash 为 32 位哈希的行")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = missing_rows(args.container, args.database, only_collected=args.only_collected)
    if args.limit:
        rows = rows[: args.limit]
    print(f"缺 author {len(rows)} 条" + ("（dry-run）" if args.dry_run else ""), flush=True)
    if not rows:
        return 0

    started = time.time()
    fixed = failed = done = 0
    pending_sql: list[str] = []

    def flush() -> None:
        nonlocal pending_sql
        if not pending_sql or args.dry_run:
            pending_sql = []
            return
        psql("begin;" + "".join(pending_sql) + "commit;", args.container, args.database)
        pending_sql = []

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(lookup, code, args.flaresolverr): (rid, code) for rid, code in rows}
        for future, (rid, code) in futures.items():
            author = future.result()
            done += 1
            if not author:
                failed += 1
                print(f"[{done}/{len(rows)}] {code} 没查到演员", flush=True)
                continue
            fixed += 1
            if not args.dry_run:
                pending_sql.append(
                    f"update magnet_links set author='{author.replace(chr(39), chr(39) * 2)}' "
                    f"where id={rid} and coalesce(author,'')='';"
                )
                if len(pending_sql) >= 50:
                    flush()
            if done % 20 == 0 or done == len(rows):
                rate = done / max(0.001, time.time() - started)
                print(f"[{done}/{len(rows)}] 命中 {fixed} 失败 {failed} ({rate:.1f} 条/秒)", flush=True)
    flush()

    print(f"完成：补上 {fixed}，未查到 {failed}，用时 {time.time() - started:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
