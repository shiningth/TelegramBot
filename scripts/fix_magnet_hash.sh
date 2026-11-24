#!/usr/bin/env bash
# 一条命令修库：把旧版镜像写坏的 magnet_hash（40 位 infohash）改回资源 slug。
#
# 症状：同一部片进不了第二个板块（CSV 导入「跳过」偏多、板块不完整），
#      因为 2026.11.24.2 之前的 csv_prepare.py 会把 magnet_hash 覆盖成 BT infohash，
#      撞上 momo 的 uq_magnet_hash_source_site 唯一索引。
#
# 用法（在跑 momo 的那台机器上执行）：
#   bash fix_magnet_hash.sh                # 只看有多少行要改（不动数据）
#   bash fix_magnet_hash.sh --apply        # 真改（先自动备份到 /tmp/magnet_hash_backup.csv）
#   bash fix_magnet_hash.sh --apply --container momo-postgres --db sehuatang_db --user postgres
#
# 判据：1080xin 的资源行 url 形如 https://1080xin.com/resources/<slug>，
#      把 magnet_hash 设成那个 <slug>；同 source 已有该 slug 的行会跳过（不撞唯一索引）。
# 幂等：跑完再跑就是「候选 0 行」。

set -euo pipefail

CONTAINER="momo-postgres"
DB="sehuatang_db"
DBUSER="postgres"
APPLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --container) CONTAINER="$2"; shift 2 ;;
    --db) DB="$2"; shift 2 ;;
    --user) DBUSER="$2"; shift 2 ;;
    -h|--help) sed -n '1,20p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

psql_run() { docker exec -i "$CONTAINER" psql -U "$DBUSER" -d "$DB" -At -F '|' "$@"; }

CANDIDATE_SQL="select count(*) from magnet_links m where m.url like '%/resources/%' and m.magnet_hash ~ '^[0-9a-f]{40}\$' and not exists (select 1 from magnet_links x where x.source_site = m.source_site and x.magnet_hash = split_part(split_part(m.url, '/resources/', 2), '?', 1) and x.id <> m.id);"
CONFLICT_SQL="select count(*) from magnet_links m where m.url like '%/resources/%' and m.magnet_hash ~ '^[0-9a-f]{40}\$' and exists (select 1 from magnet_links x where x.source_site = m.source_site and x.magnet_hash = split_part(split_part(m.url, '/resources/', 2), '?', 1) and x.id <> m.id);"

echo "[1/3] 检查容器 $CONTAINER 是否可连…"
if ! docker exec -i "$CONTAINER" psql -U "$DBUSER" -d "$DB" -At -c 'select 1' >/dev/null 2>&1; then
  echo "连不上：确认容器名/库名/用户，或在跑 momo 的机器上执行。" >&2
  exit 1
fi

READY=$(psql_run -c "$CANDIDATE_SQL")
SKIP=$(psql_run -c "$CONFLICT_SQL")
echo "[2/3] 待修 $READY 行（其中 $SKIP 行因同 source 已有该 slug 会跳过）"

if [ "$APPLY" -ne 1 ]; then
  echo "[3/3] 仅预览，未修改。确认无误后加 --apply 执行。"
  exit 0
fi

if [ "$READY" = "0" ]; then
  echo "[3/3] 无需修改。"
  exit 0
fi

echo "[3/3] 备份到容器内 /tmp/magnet_hash_backup.csv 并执行更新…"
psql_run -c "\\copy (select id, source_site, forum_id, magnet_hash, url from magnet_links where url like '%/resources/%' and magnet_hash ~ '^[0-9a-f]{40}\$') to '/tmp/magnet_hash_backup.csv' with (format csv, header true)" >/dev/null
docker cp "$CONTAINER:/tmp/magnet_hash_backup.csv" ./magnet_hash_backup.csv
echo "备份已取回：$(pwd)/magnet_hash_backup.csv"

psql_run -c "begin;
update magnet_links m
set magnet_hash = split_part(split_part(m.url, '/resources/', 2), '?', 1)
where m.url like '%/resources/%'
  and m.magnet_hash ~ '^[0-9a-f]{40}\$'
  and not exists (
    select 1 from magnet_links x
    where x.source_site = m.source_site
      and x.magnet_hash = split_part(split_part(m.url, '/resources/', 2), '?', 1)
      and x.id <> m.id
  );
commit;"

LEFT=$(psql_run -c "$CANDIDATE_SQL")
echo "完成，剩余待修 $LEFT 行（应为 0）。"
