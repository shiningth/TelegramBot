-- 修复 magnet_hash 被改写成 BT infohash 的历史行（2026.11.24.2 之前的镜像留下的）
--
-- 背景：旧版 telegram_bot/shared/csv_prepare.py 为排重把每行 magnet_hash 覆盖成磁链指纹，
--      BT 行因此变成 40 位 infohash，撞上 uq_magnet_hash_source_site，
--      导致同一部片无法在第二个板块出现（板块不完整）。
-- 判据：1080xin 的资源行 url 形如 https://1080xin.com/resources/<slug>，
--      magnet_hash 是 40 位十六进制（旧版写入的 infohash）→ 改回 slug。
-- 幂等：跑完再跑匹配 0 行；遇到同 source 已有该 slug 的行会跳过，不会撞唯一索引。
--
-- 用法（先看数字，再决定是否提交）：
--   docker exec -i MOMO-POSTGRES psql -U postgres -d sehuatang_db -f /app/scripts/fix_magnet_hash.sql
--   加 --dry-run 之类参数不存在，故本脚本默认「只统计不修改」；
--   把末尾的 BEGIN/COMMIT 保留并把 ROLLBACK 换成 COMMIT 才会真的写。

\pset pager off

-- 候选（会被改的行）
select 'candidates' as metric, count(*) as rows
from magnet_links m
where m.url like '%/resources/%'
  and m.magnet_hash ~ '^[0-9a-f]{40}$';

-- 其中会撞唯一索引的（跳过，不动）
select 'conflicts_skipped' as metric, count(*) as rows
from magnet_links m
where m.url like '%/resources/%'
  and m.magnet_hash ~ '^[0-9a-f]{40}$'
  and exists (
    select 1 from magnet_links x
    where x.source_site = m.source_site
      and x.magnet_hash = split_part(split_part(m.url, '/resources/', 2), '?', 1)
      and x.id <> m.id
  );

begin;

update magnet_links m
set magnet_hash = split_part(split_part(m.url, '/resources/', 2), '?', 1)
where m.url like '%/resources/%'
  and m.magnet_hash ~ '^[0-9a-f]{40}$'
  and not exists (
    select 1 from magnet_links x
    where x.source_site = m.source_site
      and x.magnet_hash = split_part(split_part(m.url, '/resources/', 2), '?', 1)
      and x.id <> m.id
  );

-- 默认回滚：确认数字无误后再把下面这行改成 COMMIT
rollback;
