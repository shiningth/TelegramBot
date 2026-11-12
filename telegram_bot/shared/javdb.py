"""元数据抓取：按 DMM → AVbase → AVWikiDB → Jav321 → MGStage → JavDB 的顺序取封面与演员。

只负责「新条目」的封面与演员，不做历史批量回补；任何一源失败都不影响主流程。
"""

from __future__ import annotations

import html
import logging
import re
import time
from typing import Any
from urllib.parse import quote, unquote

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_VALUE = r'<strong>{label}:?</strong>\s*(?:&nbsp;)?\s*<span class="value">(.*?)</span>'
_COVER_PATTERNS = (
    r'<img[^>]+class="video-cover"[^>]+src="([^"]+)"',
    r'class="column-video-cover".*?<img[^>]+src="([^"]+)"',
)
_ACTOR_SPLIT = re.compile(r"[,、，/]")
_JAVDB_BASE = "https://javdb.com"


def _field(page: str, label: str) -> str:
    match = re.search(_VALUE.format(label=label), page, re.S)
    if not match:
        return ""
    raw = re.sub(r"<[^>]+>", "", html.unescape(match.group(1)))
    return " ".join(raw.split()).strip()


def parse_detail(page: str) -> dict[str, str]:
    """从详情页 HTML 里取 author / cover_url / code / release_date。"""
    actors = _field(page, "演員") or _field(page, "演员")
    author = " ".join(part.strip() for part in _ACTOR_SPLIT.split(actors) if part.strip())
    cover = ""
    for pattern in _COVER_PATTERNS:
        match = re.search(pattern, page, re.S)
        if match:
            cover = html.unescape(match.group(1)).strip()
            break
    return {
        "author": author,
        "cover_url": cover,
        "code": _field(page, "番號") or _field(page, "番号"),
        "release_date": _field(page, "日期") or _field(page, "發行日期") or _field(page, "发行日期"),
    }


def fetch_via_flaresolverr(url: str, endpoint: str, *, timeout: float = 90.0) -> str:
    """通过 FlareSolverr 取页面 HTML（用于过 Cloudflare 挑战）；失败返回空串。"""
    if not endpoint or not url:
        return ""
    import httpx

    body = {"cmd": "request.get", "url": url, "maxTimeout": int(timeout * 1000)}
    try:
        with httpx.Client(timeout=timeout + 15, follow_redirects=True) as client:
            response = client.post(endpoint.rstrip("/") + "/v1", json=body)
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.info("FlareSolverr 请求失败 %s: %s", url, exc)
        return ""
    if str(payload.get("status")) != "ok":
        logger.info("FlareSolverr 未成功 %s: %s", url, str(payload.get("message"))[:120])
        return ""
    html_text = ((payload.get("solution") or {}).get("response")) or ""
    return html_text


def _javdb_meta(
    url: str,
    *,
    code: str = "",
    flaresolverr: str = "",
    timeout: float = 20.0,
    retries: int = 3,
    sleep: Any = time.sleep,
    client: Any = None,
) -> dict[str, str] | None:
    """从 JavDB 详情页取 author / cover_url / release_date；失败返回 None。"""
    if url and flaresolverr:
        html_text = fetch_via_flaresolverr(url, flaresolverr)
        if html_text:
            data = parse_detail(html_text)
            if data.get("cover_url") or data.get("author"):
                return data
        logger.info("FlareSolverr 没拿到数据：%s", url)

    if url and not flaresolverr and not _javdb_blocked():
        attempts = max(1, int(retries))
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                if client is not None:
                    response = client.get(url, headers={"User-Agent": _UA}, timeout=timeout)
                else:
                    with httpx.Client(follow_redirects=True) as own:
                        response = own.get(url, headers={"User-Agent": _UA}, timeout=timeout)
                if response.status_code == 403:
                    _mark_javdb_blocked()
                    raise RuntimeError("HTTP 403（触发风控，进入熔断）")
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}")
                data = parse_detail(response.text)
                if data.get("cover_url") or data.get("author"):
                    return data
                raise RuntimeError("页面里没有解析到封面/演员")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.info("JavDB 抓取失败（第 %s/%s 次）%s: %s", attempt, attempts, url, exc)
                if _javdb_blocked():
                    break
                if attempt < attempts:
                    sleep(min(9, 2 ** attempt))
        if not _javdb_blocked():
            logger.warning("JavDB 抓取放弃 %s: %s", url, last_error)
    return None


def fetch_meta(
    url: str,
    *,
    code: str = "",
    flaresolverr: str = "",
    timeout: float = 20.0,
    retries: int = 3,
    sleep: Any = time.sleep,
    client: Any = None,
    sources: Any = None,
) -> dict[str, str] | None:
    """按用户指定的源顺序抓封面与演员：DMM → AVbase → AVWikiDB → Jav321 → MGStage → JavDB。

    - 每个源只补「还缺的字段」，封面与演员都拿到就停，不再往下打。
    - javbus 已按用户要求停用（图片有防盗链，取到的封面在 momo 里常显示不出来）。
    - FlareSolverr 只给被 Cloudflare 拦的站点（AVbase / AVWikiDB / MGStage / JavDB）用；
      DMM 与 Jav321 直连即可，单条通常 1 秒内出结果。
    """
    if str(code or "").strip():
        normalized = re.sub(r"[^A-Za-z0-9]", "", str(code)).lower()
        if re.fullmatch(r"fc2\d+", normalized):
            # FC2 没有这类条目，避免无谓请求
            return None

    order = tuple(sources or _META_ORDER)
    # 用户 2026-11-10：「把 AVWikiDB、AVbase 也用上补演员，速度慢点也没事」。
    # 于是分两轮：always 里的源（DMM 出封面、AVbase 出演员）每次都跑并合并演员；
    # 剩下的慢源只在还缺字段时才按顺序补，避免每条都等半分钟。
    always = tuple(name for name in order if str(name).strip().lower() in _ALWAYS_SOURCES)
    rest = tuple(name for name in order if str(name).strip().lower() not in _ALWAYS_SOURCES)
    cover = ""
    author = ""
    used: list[str] = []

    def _run(names: tuple[str, ...], *, stop_when_complete: bool = True) -> None:
        nonlocal cover, author
        for name in names:
            if stop_when_complete and cover and author:
                return
            handler = _SOURCE_FUNCS.get(str(name).strip().lower())
            if handler is None:
                continue
            budget = _SOURCE_TIMEOUTS.get(str(name).strip().lower(), timeout)
            try:
                data = handler(
                    url=url,
                    code=code,
                    flaresolverr=flaresolverr,
                    timeout=budget,
                    retries=retries,
                    sleep=sleep,
                    client=client,
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("元数据源 %s 失败：%s", name, exc)
                continue
            if not data:
                continue
            added = False
            if not cover and data.get("cover_url"):
                cover = data["cover_url"]
                added = True
            if data.get("author"):
                merged = _merge_author(author, data["author"])
                if merged != author:
                    author = merged
                    added = True
            if added:
                used.append(str(name))

    _run(always, stop_when_complete=False)
    if not (cover and author):
        _run(rest)

    if not cover and not author:
        return None
    return {"author": author, "cover_url": cover, "source": used[0] if used else str(order[0])}



# ---------------------------------------------------------------- DMM 备用源
_DMM_BASE = "https://pics.dmm.co.jp"
_DMM_SEARCH_URL = "https://www.dmm.co.jp/search/=/searchstr={q}/"
# 搜索页结果卡里直接写「出演者：神菜美まい」，比走详情页快得多（约 0.7 秒）
_DMM_ACTOR = re.compile(r"出演者[：:]\s*([^<\n]{1,40})")
_DMM_PATHS = (
    "/digital/video/{c}/{c}pl.jpg",
    "/mono/movie/adult/{c}/{c}pl.jpg",
)
_DMM_CID_PATHS = _DMM_PATHS
_DMM_CID = re.compile(r"cid=([0-9a-z_]+)")
# pl.jpg 是 800×538 横版封面（与 momo 现有数据同款）；ps.jpg 只有 147×200，不可用
# 同时挡掉 now_printing 占位图（约 19KB，正常封面约 160KB）
_DMM_MIN_BYTES = 60000


def code_variants(code: str) -> list[str]:
    """站点前缀番号的搜索变体：259LUXU-1573 → ['259LUXU-1573', 'LUXU-1573']。"""
    base = str(code or "").strip().upper()
    if not base:
        return []
    variants = [base]
    match = re.fullmatch(r"\d{3}([A-Z]+-\d+)", base)
    if match:
        variants.append(match.group(1))
    return variants


def dmm_code(code: str) -> str:
    """SSIS-858 → ssis00858：DMM 的番号写法是纯小写字母数字。"""
    text = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).lower()
    match = re.fullmatch(r"([a-z]+)(\d+)", text)
    if not match:
        return text
    prefix, number = match.groups()
    return f"{prefix}{int(number):05d}"


def _dmm_cid_matches(cid: str, code: str) -> bool:
    """118abf165 是否就是 ABF-165：cid 里含字母前缀 + 数字，前缀可能带厂商编号。"""
    text = str(cid or "").lower()
    normalized = dmm_code(code)
    match = re.fullmatch(r"([a-z]+)(\d+)", normalized)
    if not match:
        return normalized in text
    prefix, number = match.groups()
    tail = f"{prefix}{int(number)}"
    return text.endswith(tail) or text.endswith(f"_{tail}")


def _dmm_search_cids(code: str, *, client: Any, timeout: float) -> list[str]:
    """DMM 搜索页里与番号对应的 cid（mono/dvd 类作品的 cid 带厂商前缀，如 118abf165）。"""
    url = "https://www.dmm.co.jp/search/=/searchstr=%s/" % quote(str(code).replace("-", ""))
    try:
        response = client.get(
            url,
            headers={
                "User-Agent": _BROWSER_UA,
                "Referer": "https://www.dmm.co.jp/",
                "Cookie": "age_check_done=1",
            },
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        return []
    if getattr(response, "status_code", 0) != 200:
        return []
    seen: list[str] = []
    for cid in _DMM_CID.findall(getattr(response, "text", "") or ""):
        if cid in seen or not _dmm_cid_matches(cid, code):
            continue
        seen.append(cid)
    return seen


def fetch_dmm_cover(code: str, *, timeout: float = 15.0, client: Any = None) -> str:
    """按番号找 DMM 封面，返回可访问的那一个；没有就返回空串。

    先猜 digital/video 与 mono/movie/adult 的常规写法（1 次请求内），
    猜不到再去 DMM 搜索页拿真实 cid（mono/dvd 类作品是 118abf165 这种带厂商前缀的写法）。
    """
    import httpx

    normalized = [item for item in (dmm_code(v) for v in code_variants(code)) if item]
    if not normalized:
        return ""
    headers = {"User-Agent": _BROWSER_UA, "Referer": "https://www.dmm.co.jp/"}
    own_client = None
    try:
        if client is None:
            own_client = httpx.Client(follow_redirects=True, timeout=timeout)
            client = own_client

        def _try(cid: str) -> str:
            for path in _DMM_CID_PATHS:
                url = _DMM_BASE + path.format(c=cid)
                try:
                    response = client.get(url, headers=headers, timeout=timeout)
                    body = getattr(response, "content", b"") or b""
                except Exception:  # noqa: BLE001
                    continue
                if getattr(response, "status_code", 0) == 200 and len(body) > _DMM_MIN_BYTES:
                    return url
            return ""

        for cid in normalized:
            found = _try(cid)
            if found:
                return found
        for cid in _dmm_search_cids(code, client=client, timeout=timeout):
            found = _try(cid)
            if found:
                return found
    finally:
        if own_client is not None:
            own_client.close()
    return ""


# ---------------------------------------------------------------- 多源元数据链
# 用户 2026-11-10 指定顺序：DMM → AVbase → AVWikiDB → Jav321 → MGStage → JavDB
# javbus 已停用：它的封面有防盗链（无 Referer 返回 403），拿到的图在 momo 里显示不出来。
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_META_ORDER: tuple[str, ...] = ("dmm", "avbase", "avwikidb", "jav321", "mgstage", "javdb")
# 直连即可的快速源：先跑，够了就不碰慢源
_FAST_SOURCES: frozenset[str] = frozenset({"dmm", "jav321"})
# 每轮都跑并合并演员的源：DMM 给封面与主演员（约 1–5 秒），AVbase 给完整演员表（约 0.5–1.6 秒）
_ALWAYS_SOURCES: frozenset[str] = frozenset({"dmm", "avbase"})
# 每个源的上限：被 Cloudflare 拦的几个只能等这么久，等不到就当没有，避免拖住整条采集
_SOURCE_TIMEOUTS: dict[str, float] = {
    "dmm": 12.0,
    "avbase": 12.0,
    "avwikidb": 15.0,
    "jav321": 10.0,
    "mgstage": 10.0,
    "javdb": 40.0,
}
_AVBASE_BASE = "https://www.avbase.net"
_AVWIKI_BASE = "https://avwikidb.com"
_JAV321_BASE = "https://www.jav321.com"
_MGSTAGE_BASE = "https://www.mgstage.com"

_AVWIKI_ACTRESS = re.compile(r"女優は\s*([^｜<\n]{1,60})")
# AVbase 把作品数据塞在页面里的 JSON 中：`"actors":[{"id":46092,"name":"miru",...}]`
_AVBASE_ACTORS = re.compile(r'"actors"\s*:\s*\[(.*?)\]', re.S)
_AVBASE_ACTOR_NAME = re.compile(r'"name"\s*:\s*"([^"]{1,60})"')
_AVBASE_TALENT = re.compile(r'href="/talents/([^"]+)"')
_AVBASE_COVER = re.compile(r'https://pics\.dmm\.co\.jp/[^"\s\\]+?pl\.jpg')
_AVBASE_TALENT = re.compile(r'href="/(?:talents?|actress)/([^"]+)"[^>]*>\s*([^<]{1,40})<')
_JAV321_ACTRESS = re.compile(r"<b>出演者</b>:\s*(.*?)<br>", re.S)
_JAV321_ACTOR_NAME = re.compile(r">([^<]+)</a>")
_MGSTAGE_COVER = re.compile(r"https://image\.mgstage\.com/images/[^\"'\s]+?pb_e_[^\"'\s]+?\.jpg")
_MGSTAGE_ACTRESS = re.compile(r"出演者</[^>]+>(.{0,400}?)</tr>", re.S)

_JAVDB_BLOCKED_UNTIL = 0.0
_JAVDB_BLOCK_HOURS = 6.0


def _javdb_blocked() -> bool:
    return time.time() < _JAVDB_BLOCKED_UNTIL


def _mark_javdb_blocked() -> None:
    global _JAVDB_BLOCKED_UNTIL
    _JAVDB_BLOCKED_UNTIL = time.time() + _JAVDB_BLOCK_HOURS * 3600
    logger.warning("JavDB 返回 403，暂停直连抓取 %.0f 小时（有 FlareSolverr 时仍可走它）", _JAVDB_BLOCK_HOURS)


def _clean(text: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).split()).strip()


def _merge_author(current: str, incoming: str) -> str:
    """合并多源演员名（DMM 主演员 + AVbase 全表），去重后按出现顺序拼起来。"""
    names: list[str] = []
    for chunk in (current, incoming):
        for name in re.split(r"[\s、,，/]+", str(chunk or "")):
            name = name.strip()
            if name and name not in names:
                names.append(name)
    return " ".join(names)


def _pl_cover(page: str, slug: str) -> str:
    """页面上出现过的该番号 DMM 横版封面（800×538）。"""
    if not slug:
        return ""
    pattern = re.compile(r"https://pics\.dmm\.co\.jp/digital/video/%s/%spl\.jpg" % (slug, slug), re.I)
    match = pattern.search(page or "")
    return match.group(0) if match else ""


def fetch_dmm_meta(code: str, *, timeout: float = 15.0, client: Any = None) -> dict[str, str]:
    """DMM 一次拿齐封面与演员，返回 {cover_url, author, source}（可能为空串）。

    搜索页的结果卡直接写「出演者：xxx」，所以一条请求就能拿到演员（约 0.7 秒）；
    封面仍走 pics.dmm.co.jp 的 pl.jpg（800×538），mono/dvd 类靠搜索页的 cid 兜底。
    """
    result = {"cover_url": "", "author": "", "source": "dmm"}
    if not code:
        return result
    import httpx

    own = None
    headers = {
        "User-Agent": _BROWSER_UA,
        "Referer": "https://www.dmm.co.jp/",
        "Accept-Language": "ja,en;q=0.8",
        "Cookie": "age_check_done=1",
    }
    try:
        if client is None:
            own = httpx.Client(follow_redirects=True, timeout=timeout)
            client = own
        try:
            response = client.get(
                _DMM_SEARCH_URL.format(q=quote(str(code).replace("-", ""))),
                headers=headers,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("DMM 搜索页失败 %s: %s", code, exc)
            response = None
        page = getattr(response, "text", "") or "" if getattr(response, "status_code", 0) == 200 else ""
        hit = _DMM_ACTOR.search(page)
        if hit:
            result["author"] = _clean(hit.group(1))
        result["cover_url"] = fetch_dmm_cover(code, timeout=timeout, client=client)
    finally:
        if own is not None:
            own.close()
    return result


def _fetch_dmm(*, url: str = "", code: str = "", timeout: float = 15.0, client: Any = None, **_ignored: Any) -> dict[str, str] | None:
    if not code:
        return None
    data = fetch_dmm_meta(code, timeout=timeout, client=client)
    if not (data["cover_url"] or data["author"]):
        return None
    return data


def _avbase_author(page: str) -> str:
    """AVbase 的演员在页面内嵌 JSON 里：`"actors":[{"name":"miru",…}]`；老结构退回 /talents/ 链接。"""
    block = _AVBASE_ACTORS.search(page or "")
    if block:
        names: list[str] = []
        for name in _AVBASE_ACTOR_NAME.findall(block.group(1)):
            cleaned = _clean(name)
            if cleaned and cleaned not in names:
                names.append(cleaned)
        if names:
            return " ".join(names)
    slug = _AVBASE_TALENT.search(page or "")
    if slug:
        return _clean(unquote(slug.group(1))).replace("-", " ")
    return ""


def _fetch_avbase(*, code: str = "", flaresolverr: str = "", timeout: float = 30.0, client: Any = None, **_ignored: Any) -> dict[str, str] | None:
    """AVbase（Cloudflare）：页面内嵌 JSON 同时给出演员与 DMM 封面。"""
    if not code or not flaresolverr:
        return None
    page = fetch_via_flaresolverr(
        "%s/works?q=%s" % (_AVBASE_BASE, quote(code)), flaresolverr, timeout=timeout
    )
    if not page:
        return None
    cover = _AVBASE_COVER.search(page)
    author = _avbase_author(page)
    return {"cover_url": cover.group(0) if cover else "", "author": author, "source": "avbase"} if (cover or author) else None


def _fetch_avwikidb(*, code: str = "", flaresolverr: str = "", timeout: float = 30.0, client: Any = None, **_ignored: Any) -> dict[str, str] | None:
    """AVWikiDB 作品页直接写「女優は ○○」，同时带 DMM 封面（约 17 秒，只能走 FlareSolverr）。"""
    if not code or not flaresolverr:
        return None
    page = fetch_via_flaresolverr(
        "%s/work/%s/" % (_AVWIKI_BASE, quote(str(code).upper())), flaresolverr, timeout=timeout
    )
    if not page:
        return None
    hit = _AVWIKI_ACTRESS.search(page)
    author = _clean(hit.group(1)) if hit else ""
    cover = _pl_cover(page, dmm_code(code))
    return {"cover_url": cover, "author": author, "source": "avwikidb"} if (cover or author) else None


def _jav321_slugs(code: str) -> list[str]:
    """Jav321 有两套 URL 写法：S1 系 ssis00858、Prestige 系 abf-165。"""
    out: list[str] = []
    for slug in (dmm_code(code), str(code or "").strip().lower()):
        if slug and slug not in out:
            out.append(slug)
    return out


def _jav321_names(page: str) -> list[str]:
    """出演者：老页面是 <a href="/star/..">名</a>，新页面是纯文本「名 &nbsp;」（可能带年龄/职业）。"""
    block = _JAV321_ACTRESS.search(page or "")
    if not block:
        return []
    html = block.group(1)
    anchored = _JAV321_ACTOR_NAME.findall(html)
    if anchored:
        candidates = anchored
    else:
        text = re.sub(r"<[^>]+>", " ", html).replace("&nbsp;", " ").strip()
        candidates = text.split()[:1]
    names: list[str] = []
    for name in candidates:
        cleaned = _clean(name)
        if cleaned and cleaned not in names:
            names.append(cleaned)
    return names


def _jav321_cover(page: str) -> str:
    """Jav321 自家图床的竖版封面（实测无防盗链，裸取 200）。"""
    hit = re.search(r'src="(https://www\.jav321\.com/images/[^"]+?\.jpg)"', page or "")
    return hit.group(1) if hit else ""


def _fetch_jav321(*, code: str = "", timeout: float = 12.0, client: Any = None, **_ignored: Any) -> dict[str, str] | None:
    """Jav321 详情页直连（约 0.7 秒）：页面已不再写「出演者」，现在只当封面兜底。"""
    if not code:
        return None
    import httpx

    slugs = _jav321_slugs(code)
    if not slugs:
        return None
    own = None
    page = ""
    slug = slugs[0]
    try:
        if client is None:
            own = httpx.Client(follow_redirects=True, timeout=timeout)
            client = own
        for candidate in slugs:
            url = "%s/video/%s" % (_JAV321_BASE, candidate)
            try:
                resp = client.get(url, headers={"User-Agent": _BROWSER_UA, "Accept-Language": "ja,en;q=0.8"})
            except Exception as exc:  # noqa: BLE001
                logger.info("Jav321 抓取失败 %s: %s", url, exc)
                continue
            if getattr(resp, "status_code", 0) == 200:
                page = getattr(resp, "text", "") or ""
                slug = candidate
                break
    finally:
        if own is not None:
            own.close()
    if not page:
        return None
    names = _jav321_names(page)
    cover = _pl_cover(page, slug) or _jav321_cover(page)
    author = " ".join(names)
    return {"cover_url": cover, "author": author, "source": "jav321"} if (cover or author) else None


def _fetch_mgstage(*, code: str = "", flaresolverr: str = "", timeout: float = 25.0, client: Any = None, **_ignored: Any) -> dict[str, str] | None:
    """MGStage 自家番号（MGSMPL 之类）：作品页有 mgstage 图床封面与演员。"""
    if not code:
        return None
    target = "%s/product/product_detail/%s/" % (_MGSTAGE_BASE, quote(str(code).upper()))
    page = ""
    if flaresolverr:
        page = fetch_via_flaresolverr(target, flaresolverr, timeout=timeout)
    if not page:
        return None
    cover = ""
    match = _MGSTAGE_COVER.search(page)
    if match:
        cover = match.group(0)
    author = ""
    block = _MGSTAGE_ACTRESS.search(page)
    if block:
        names = [item for item in _JAV321_ACTOR_NAME.findall(block.group(1))]
        author = " ".join(dict.fromkeys(_clean(n) for n in names if _clean(n)))
    return {"cover_url": cover, "author": author, "source": "mgstage"} if (cover or author) else None


def _fetch_javdb(
    *,
    url: str = "",
    code: str = "",
    flaresolverr: str = "",
    timeout: float = 20.0,
    retries: int = 3,
    sleep: Any = time.sleep,
    client: Any = None,
    **_ignored: Any,
) -> dict[str, str] | None:
    """JavDB（最慢的一档：过 Cloudflare 常要 60–75 秒），只在前面的源都拿不全时用。"""
    if url:
        return _javdb_meta(
            url,
            code=code,
            flaresolverr=flaresolverr,
            timeout=timeout,
            retries=retries,
            sleep=sleep,
            client=client,
        )
    if not code:
        return None
    return search_detail_by_code(code, flaresolverr=flaresolverr, timeout=timeout, client=client)


_SOURCE_FUNCS = {
    "dmm": _fetch_dmm,
    "avbase": _fetch_avbase,
    "avwikidb": _fetch_avwikidb,
    "jav321": _fetch_jav321,
    "mgstage": _fetch_mgstage,
    "javdb": _fetch_javdb,
}


def _fetch_javdb_html(url: str, *, flaresolverr: str = "", timeout: float = 20.0, client: Any = None) -> str:
    """取 JavDB 页面 HTML：配了 FlareSolverr 就走它（过 Cloudflare），否则直连。"""
    if flaresolverr:
        return fetch_via_flaresolverr(url, flaresolverr, timeout=max(timeout, 60.0)) or ""
    import httpx

    try:
        if client is not None:
            resp = client.get(url, headers={"User-Agent": _UA})
        else:
            with httpx.Client(follow_redirects=True, timeout=timeout) as own:
                resp = own.get(url, headers={"User-Agent": _UA})
    except Exception as exc:  # noqa: BLE001
        logger.info("JavDB 直连失败 %s: %s", url, exc)
        return ""
    if resp.status_code == 403:
        _mark_javdb_blocked()
        return ""
    if resp.status_code >= 400:
        return ""
    return resp.text or ""


def search_detail_by_code(
    code: str,
    *,
    flaresolverr: str = "",
    timeout: float = 20.0,
    hits: int = 4,
    client: Any = None,
) -> dict[str, str] | None:
    """按番号搜索 JavDB，命中同番号的详情页后解析元数据（消息不必带链接）。

    - 配置了 FlareSolverr 时走它，否则直连（直连在本机 IP 上是 403）。
    - 搜索页本身就带 jdbstatic 封面，命中时可直接取；详情页用于校验番号。
    """
    if not str(code or "").strip():
        return None
    if not flaresolverr and _javdb_blocked():
        return None

    for variant in code_variants(code):
        target = variant.strip().upper()
        url = "%s/search?q=%s&f=all" % (_JAVDB_BASE, quote(target))
        html_search = _fetch_javdb_html(url, flaresolverr=flaresolverr, timeout=timeout, client=client)
        if not html_search:
            if not flaresolverr and _javdb_blocked():
                return None
            continue
        hrefs: list[str] = []
        for href in re.findall(r'<a href="(/v/[A-Za-z0-9]+)"', html_search):
            if href not in hrefs:
                hrefs.append(href)
        pattern = re.compile(r"(?<![A-Za-z0-9])%s(?![0-9])" % re.escape(target))
        # 搜索结果里 <strong> 标的就是番号，先用它快速筛，筛不到再逐个开详情页
        for href in hrefs[: max(1, hits)]:
            html_detail = _fetch_javdb_html(_JAVDB_BASE + href, flaresolverr=flaresolverr, timeout=timeout, client=client)
            if not html_detail:
                continue
            match = re.search(r"<title>(.*?)</title>", html_detail, re.S)
            title = (match.group(1) if match else "").upper()
            if not pattern.search(title):
                continue
            data = parse_detail(html_detail)
            if data.get("cover_url"):
                return data
    return None
