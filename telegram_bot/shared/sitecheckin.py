"""网页签到通用实现：用 Cookie 直接请求站点签到页/接口。

设计原则：
1. 站点无关——所有差异都走配置（url / method / sign_url / headers / 关键字）。
2. 先直连，只有明确被 Cloudflare 拦住才回落 FlareSolverr。
3. 任何一步失败都返回可读原因，不抛异常给调度器。
"""

from __future__ import annotations

import hashlib
import json
import html as html_lib
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from telegram_bot.shared import netproxy

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_CF_MARKERS = ("Just a moment", "cf-browser-verification", "challenges.cloudflare.com", "Attention Required")
_LOGIN_MARKERS = (
    "login.php",
    "takelogin",
    "请先登录",
    "请登录后",
    "你还没有登录",
    "未登录",
    "必须在登录后",
    "该页面必须在登录",
)
_ALREADY_MARKERS = (
    "已经签到",
    "已签到",
    "请勿重复",
    "今天已签到",
    "今日已签到",
    "您今天已经",
    "已打过卡",
    "今天已经签到",
)
_SIGNED_MARKERS = ("签到成功", "签到已得", "本次签到获得", "签到奖励", "魔力值增加")
_TURNSTILE_MARKERS = ("cf-turnstile", "data-sitekey", "challenges.cloudflare.com/turnstile")
_COUNT_RE = re.compile(r"第\s*(\d+)\s*次签到")

_FORM_RE = re.compile(r"<form\b[\s\S]*?</form>", re.IGNORECASE)
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
_SLIDER_JS_RE = re.compile(r'src="(/slide_check[^"]+)"')
_TOKEN_URL_RE = re.compile(r"https?://[^\"'\s]+set_access_token-[^\"'\s]+")


@dataclass
class SignResult:
    state: str  # signed | already | failed
    detail: str = ""
    http: int = 0
    final_url: str = ""
    elapsed_ms: int = 0
    debug_html: str = ""
    page_text: str = ""
    dates: tuple[str, ...] = ()
    bonus: str = ""

    @property
    def ok(self) -> bool:
        return self.state in ("signed", "already")


def parse_cookie(text: str) -> dict[str, str]:
    """把浏览器里复制的整段 Cookie 拆成字典。"""
    cookies: dict[str, str] = {}
    for chunk in re.split(r"[;\n]", str(text or "")):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


def parse_headers(text: str) -> dict[str, str]:
    """解析 `Name:value;Name2:value2` 形式的额外请求头。"""
    headers: dict[str, str] = {}
    for chunk in re.split(r"[;\n]", str(text or "")):
        if ":" not in chunk:
            continue
        name, value = chunk.split(":", 1)
        name = name.strip()
        if name:
            headers[name] = value.strip()
    return headers


def parse_pairs(text: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for chunk in re.split(r"[&\n]", str(text or "")):
        if "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        if name.strip():
            data[name.strip()] = value.strip()
    return data


def page_text(html: str) -> str:
    body = re.sub(r"<script[\s\S]*?</script>", " ", html or "")
    body = re.sub(r"<style[\s\S]*?</style>", " ", body)
    body = re.sub(r"<[^>]+>", " ", body)
    body = html_lib.unescape(body)
    body = body.replace("\xa0", " ")
    return re.sub(r"\s+", " ", body).strip()


def _keywords(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,，\n]", str(text or "")) if item.strip()]


def _snippet(text: str, word: str, width: int = 48) -> str:
    """把命中关键字附近的一小段文字带回去，日志里能看出站点到底说了什么。"""
    index = text.find(word) if word else -1
    if index < 0:
        return word
    best = ""
    for match in re.finditer(re.escape(word), text):
        tail = text[match.start() : match.start() + width]
        for stop in ("当前时间", "。", "！", "!", "|"):
            pos = tail.find(stop)
            if 0 < pos:
                tail = tail[:pos]
        tail = tail.strip(" []|·-")
        if len(tail) > len(best):
            best = tail
    return best or word


_EVENT_DATE_RE = re.compile(r"""["']start["']\s*:\s*["'](\d{4}-\d{2}-\d{2})["']""")


def signed_dates(html: str) -> set[str]:
    """从页面日历脚本里取出「已签到」的日期（如 pt.0ff.cc 的 FullCalendar events）。"""
    return set(_EVENT_DATE_RE.findall(html or ""))


def _stash(result: "SignResult", html: str) -> None:
    """存调试片段；顺带把日历里的已签日期抠出来（整页 45KB，截断后就取不到了）。"""
    result.debug_html = (html or "")[:6000]
    result.dates = tuple(sorted(signed_dates(html)))


def _stale_page(state: str, dates: tuple[str, ...], today: str) -> bool:
    """页面写着「签到成功」但日历里没有今天 → 多半只是回显上一次结果。"""
    return state == "signed" and bool(dates) and today not in dates


def _json_state(text: str) -> tuple[str, str] | None:
    """站点用 ajax 返回 JSON（如 {"ret":0,"msg":"..."}）时，直接据此判定状态。"""
    body = (text or "").strip()
    if not body.startswith("{") or '"ret"' not in body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict) or "ret" not in data:
        return None
    message = str(data.get("msg") or "").strip()
    if "已经签到" in message or "已签到" in message:
        return "already", message or "今日已签到"
    if "补签卡" in message:
        # 站点把「今日签到」当补签处理（多见于还没到开签时间），不算失败
        return "pending", message or "补签卡不足，可能还没到今日签到时段"
    if str(data.get("ret")) in ("0", "1"):
        return "signed", message or "签到成功"
    return "failed", message or "站点返回失败"


def classify(
    html: str,
    *,
    final_url: str = "",
    success_keywords: str = "",
    already_keywords: str = "",
) -> tuple[str, str]:
    """判定页面状态：返回 (state, 说明)。state ∈ already / signed / login / slider / unknown。"""
    text = page_text(html)
    for word in _keywords(already_keywords) + list(_ALREADY_MARKERS):
        if word and word in text:
            return "already", _snippet(text, word)
    # 登录页的特征通常在 href 里（page_text 已剥掉标签），所以原文也要看一眼
    if "login.php" in (final_url or "") or "login.php" in (html or "") or any(
        marker in text for marker in _LOGIN_MARKERS
    ):
        return "login", "Cookie 失效或未登录"
    if "dragContainer" in html or "滑动认证" in text or "set_access_token-" in html:
        return "slider", "滑动认证"
    lowered = (html or "").lower()
    if any(marker in lowered for marker in _TURNSTILE_MARKERS):
        return "captcha", "需要 Cloudflare Turnstile 验证"
    for word in _keywords(success_keywords) + list(_SIGNED_MARKERS):
        if word and word in text:
            return "signed", _snippet(text, word)
    return "unknown", ""


def _form_fields(html: str) -> tuple[str, str, dict[str, str]] | None:
    match = _FORM_RE.search(html or "")
    if not match:
        return None
    form = match.group(0)
    head = form[: form.index(">") + 1]
    attrs = {name.lower(): value for name, value in _ATTR_RE.findall(head)}
    fields: dict[str, str] = {}
    for tag in _INPUT_RE.findall(form):
        info = {name.lower(): value for name, value in _ATTR_RE.findall(tag)}
        name = info.get("name")
        if not name:
            continue
        kind = (info.get("type") or "text").lower()
        if kind in ("submit", "button"):
            continue
        fields[name] = info.get("value", "")
    return attrs.get("action", ""), (attrs.get("method") or "post").lower(), fields


def _looks_like_sign_form(html: str) -> bool:
    parsed = _form_fields(html)
    if not parsed:
        return False
    action, _, fields = parsed
    haystack = " ".join([action, *fields.keys()]).lower()
    return any(word in haystack for word in ("attend", "sign", "checkin", "qiandao", "打卡", "签到"))


def _cf_blocked(resp: httpx.Response) -> bool:
    if resp.status_code in (403, 503, 429):
        return True
    head = (resp.text or "")[:3000]
    return any(marker in head for marker in _CF_MARKERS)


def _solve_slider(
    client: httpx.Client, html: str, base_url: str, *, attempts: int = 3
) -> str:
    """NexusPHP 系滑动认证：取 slide_check JS 里的 set_access_token 地址并请求。

    这一步能让「必须拖到底」的验证在服务端通过；站点若把 token 绑在
    真实浏览器会话上，这里会失败并如实返回，交给浏览器兜底。

    0 点重置高峰时站点偶尔会返回没有 token 的旧脚本，所以这里重试几次并
    每次都重新取签到页（滑块脚本名与 token 每次请求都会变）。
    """
    last = ""
    for index in range(max(1, attempts)):
        page = html if index == 0 else ""
        if not page:
            try:
                page = client.get(base_url).text
            except Exception as exc:  # noqa: BLE001
                last = f"取签到页失败：{type(exc).__name__}"
                time.sleep(1.0)
                continue
        match = _SLIDER_JS_RE.search(page or "")
        if not match:
            last = "页面上没有滑块脚本"
            time.sleep(1.0)
            continue
        js_url = urljoin(base_url, match.group(1))
        try:
            js = client.get(
                js_url, headers={"Cache-Control": "no-cache", "Pragma": "no-cache"}
            ).text
        except Exception as exc:  # noqa: BLE001
            last = f"取滑块脚本失败：{type(exc).__name__}"
            time.sleep(1.0)
            continue
        token = _TOKEN_URL_RE.search(js or "")
        if not token:
            last = "滑块脚本里没有找到验证地址"
            time.sleep(1.0)
            continue
        try:
            client.get(
                token.group(0),
                headers={"X-Requested-With": "XMLHttpRequest", "Accept": "*/*"},
            )
        except Exception as exc:  # noqa: BLE001
            last = f"提交滑块验证失败：{type(exc).__name__}"
            time.sleep(1.0)
            continue
        return ""
    return last


def _fetch_via_flaresolverr(url: str, endpoint: str, timeout: int) -> str:
    """用 FlareSolverr 代取页面，返回 HTML；失败返回空串。"""
    if not endpoint:
        return ""
    payload = {"cmd": "request.get", "url": url, "maxTimeout": max(20, timeout) * 1000}
    try:
        resp = httpx.post(endpoint.rstrip("/") + "/v1", json=payload, timeout=max(30, timeout + 20))
        data = resp.json()
    except Exception:  # noqa: BLE001
        return ""
    solution = (data or {}).get("solution") or {}
    return str(solution.get("response") or "")


def _trawl_scrape(
    url: str,
    endpoint: str,
    cookie: str,
    timeout: int,
    solve: bool = True,
) -> dict[str, Any]:
    """走 TRAWL 原生 /scrape（Camoufox 真浏览器 + 内置 Turnstile 支持）。

    TRAWL 的 /v1 兼容口默认跳过嵌入控件解题，只有原生 /scrape 才会处理
    Turnstile 之类的小组件。返回原始响应字典，失败返回空字典。
    """
    base = (endpoint or "").rstrip("/")
    if not base:
        return {}
    # TRAWL 只认 cookies 数组；单给 Cookie 头会被忽略（实测会掉登录态）。
    host = urlsplit(url).hostname or ""
    jar = [
        {"name": name, "value": value, "domain": host, "path": "/"}
        for name, value in parse_cookie(cookie).items()
    ]
    payload = {
        "url": url,
        "cookies": jar,
        "solveEmbeddedCaptchas": bool(solve),
        # Turnstile 首轮解题可能要 60–120 秒，给足服务端预算
        "timeout": max(90, timeout * 3) * 1000,
    }
    try:
        resp = httpx.post(
            base + "/scrape",
            json=payload,
            timeout=max(150, timeout * 4),
        )
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


_TURNSTILE_TOKEN_RE = re.compile(
    r"""name=["']cf-turnstile-response["'][^>]*value=["']([^"']+)["']""", re.I
)
_TURNSTILE_TOKEN_RE2 = re.compile(
    r"""value=["']([^"']+)["'][^>]*name=["']cf-turnstile-response["']""", re.I
)
_HIDDEN_INPUT_RE = re.compile(r"<input[^>]*type=[\"']hidden[\"'][^>]*>", re.I)


def _turnstile_payload(html: str) -> dict[str, str]:
    """从已解出 Turnstile 的页面里取出表单字段（token + 其它 hidden input）。

    真浏览器解完验证后，页面里通常已经把 token 写进 `cf-turnstile-response`
    的隐藏域；把它连同 hidden 字段一起 POST 出去，能确保签到请求真的发出，
    而不是赌页面自己的 JS 回调会提交。
    """
    text = html or ""
    token = ""
    for pattern in (_TURNSTILE_TOKEN_RE, _TURNSTILE_TOKEN_RE2):
        match = pattern.search(text)
        if match:
            token = html_lib.unescape(match.group(1)).strip()
            if token:
                break
    if not token:
        return {}
    payload: dict[str, str] = {"cf-turnstile-response": token}
    for tag in _HIDDEN_INPUT_RE.findall(text):
        name = re.search(r"""name=["']([^"']+)["']""", tag, re.I)
        value = re.search(r"""value=["']([^"']*)["']""", tag, re.I)
        if not name:
            continue
        key = name.group(1)
        if key in payload or key.startswith("cf-"):
            continue
        payload[key] = html_lib.unescape(value.group(1)) if value else ""
    return payload


def _submit_turnstile_form(
    url: str,
    html: str,
    cookie: str,
    user_agent: str,
    *,
    timeout: int = 40,
) -> str:
    """用取到的 Turnstile token 亲自 POST 一次签到表单，返回响应 HTML。"""
    payload = _turnstile_payload(html)
    if not payload:
        return ""
    headers = {"User-Agent": user_agent or DEFAULT_UA}
    try:
        with netproxy.client_for(url,
            headers=headers,
            cookies=parse_cookie(cookie),
            follow_redirects=True,
            timeout=timeout,
        ) as client:
            resp = client.post(url, data=payload)
    except Exception:  # noqa: BLE001
        return ""
    if resp.status_code >= 400:
        return ""
    return resp.text or ""


def _sign_via_browser(
    result: "SignResult",
    started: float,
    *,
    url: str,
    cookie: str,
    endpoint: str,
    timeout: int,
    success_keywords: str,
    already_keywords: str,
    today: str,
) -> "SignResult":
    """用 TRAWL 跑真浏览器：Turnstile 通过后页面自身回调会提交表单，再取一次看结果。

    注意：TRAWL 那趟即使我们这边等超时了，浏览器里的签到请求往往已经发出去并生效，
    所以超时一律落到 pending 并补一次快速复核，不能直接判失败。
    """
    solved: list[str] = []
    html = ""
    state = "unknown"
    why = ""
    for _attempt in (1, 2):
        data = _trawl_scrape(url, endpoint, cookie, timeout)
        if not data:
            state = "timeout"
            continue
        solved = [str(x) for x in (data.get("captchasSolved") or [])] or solved
        html = str(data.get("html") or html)
        result.http = int(data.get("statusCode") or result.http or 0)
        _stash(result, html)
        result.page_text = page_text(html)[:400]
        state, why = classify(
            html,
            final_url=str(data.get("url") or url),
            success_keywords=success_keywords,
            already_keywords=already_keywords,
        )
        if state in ("already", "signed") and not _stale_page(state, result.dates, today):
            result.state = state
            result.detail = why or ("今日已签到" if state == "already" else "签到成功")
            return _finish(result, started)
        if state == "login":
            result.detail = "Cookie 已失效，请重新复制"
            return _finish(result, started)
        if state == "captcha":
            # 真浏览器已经把 Turnstile 解出 token：直接带 token POST，别赌页面 JS 会自己提交
            posted = _submit_turnstile_form(url, html, cookie, str(data.get("userAgent") or ""))
            if posted:
                _stash(result, posted)
                result.page_text = page_text(posted)[:400]
                st3, why3 = classify(
                    posted,
                    final_url=url,
                    success_keywords=success_keywords,
                    already_keywords=already_keywords,
                )
                if st3 in ("already", "signed") and not _stale_page(st3, result.dates, today):
                    result.state = st3
                    result.detail = why3 or ("今日已签到" if st3 == "already" else "签到成功")
                    return _finish(result, started)
                if st3 == "login":
                    result.detail = "Cookie 已失效，请重新复制"
                    return _finish(result, started)
                state, why = st3, why3

    # 复核：不解验证、短超时，只为确认浏览器那趟是否其实已经签上了
    verify = _trawl_scrape(url, endpoint, cookie, min(timeout, 40), solve=False)
    verify_html = str((verify or {}).get("html") or "")
    if verify_html:
        _stash(result, verify_html)
        result.page_text = page_text(verify_html)[:400]
        result.http = int((verify or {}).get("statusCode") or result.http or 0)
        st2, why2 = classify(
            verify_html,
            final_url=str((verify or {}).get("url") or url),
            success_keywords=success_keywords,
            already_keywords=already_keywords,
        )
        if st2 in ("already", "signed") and not _stale_page(st2, result.dates, today):
            result.state = st2
            result.detail = (why2 or "签到成功") + (
                "（浏览器请求超时，复核确认已完成）" if state == "timeout" else ""
            )
            return _finish(result, started)
        if st2 == "login":
            result.detail = "Cookie 已失效，请重新复制"
            return _finish(result, started)

    if state == "timeout":
        result.state = "pending"
        result.detail = "浏览器请求超时，签到结果待复核（下次调度会再查）"
        return _finish(result, started)
    if state == "captcha":
        result.state = "captcha"
        extra = f"（TRAWL 已处理：{'、'.join(solved)}）" if solved else ""
        result.detail = "Turnstile 未通过" + extra
        return _finish(result, started)
    result.detail = "浏览器签到后仍未看到签到结果"
    return _finish(result, started)


_QUIZ_POSTER_RE = re.compile(r"<img[^>]+src='([^']*?/pic/attend/[^']+)'", re.I)
_QUIZ_RADIO_RE = re.compile(
    r"<input[^>]*type='radio'[^>]*name='([^']+)'[^>]*value='([^']+)'[^>]*>\s*(?:</?\w+[^>]*>)*\s*([^<]+)",
    re.I,
)
_QUIZ_SUBMIT_RE = re.compile(
    r"<input[^>]*type='submit'[^>]*name='([^']+)'[^>]*value='([^']*)'", re.I
)
_QUIZ_WRONG_MARKERS = ("回答错误", "答案错误", "选择错误", "答错", "扣除")


def load_quiz_table(path: str | Path) -> dict[str, str]:
    """读取「封面 md5 → 正确片名」表。文件不存在或格式不对时返回空表。"""
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return {}
    posters = data.get("posters") if isinstance(data, dict) else None
    if not isinstance(posters, dict):
        return {}
    table: dict[str, str] = {}
    for key, value in posters.items():
        key = str(key or "").strip().lower()
        value = str(value or "").strip()
        if key and value:
            table[key] = value
    return table


def _quiz_options(html: str) -> list[tuple[str, str, str]]:
    """返回 [(name, value, 文案)]。"""
    return [
        (m.group(1), m.group(2), html_lib.unescape(m.group(3)).strip())
        for m in _QUIZ_RADIO_RE.finditer(html)
    ]


def _quiz_pick(options: list[tuple[str, str, str]], title: str) -> tuple[str, str, str] | None:
    """按片名挑选项：先精确，再忽略空白，最后前缀。"""
    want = _squash(title)
    for opt in options:
        if opt[2] == title:
            return opt
    for opt in options:
        if _squash(opt[2]) == want:
            return opt
    for opt in options:
        if want and _squash(opt[2]).startswith(want):
            return opt
    return None


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _sign_via_quiz(
    result: SignResult,
    started: float,
    *,
    url: str,
    cookie: str,
    headers: dict[str, str],
    timeout: int,
    success_keywords: str,
    already_keywords: str,
    today: str,
    table_path: str,
    tries: int = 8,
) -> SignResult:
    """答题式签到：读题（封面）→ 查表得片名 → 选中同名选项 → 提交。

    表里没有这张封面时只「换题」，绝不瞎猜（答错会被扣魔力）。
    """
    table = load_quiz_table(table_path)
    if not table:
        result.detail = f"题库为空（{table_path}），未提交"
        return _finish(result, started)

    missing: list[str] = []
    with netproxy.client_for(url,
        cookies=parse_cookie(cookie), headers=headers, timeout=timeout, follow_redirects=True
    ) as client:
        for _ in range(max(1, tries)):
            resp = client.get(url)
            html = resp.text
            result.http = resp.status_code
            result.final_url = str(resp.url)
            result.debug_html = html
            state, why = classify(
                html,
                final_url=str(resp.url),
                success_keywords=success_keywords,
                already_keywords=already_keywords,
            )
            if state in ("already", "signed"):
                result.state = state
                result.detail = why or "签到成功"
                return _finish(result, started)
            if state == "login":
                result.detail = "Cookie 已失效，请重新复制"
                return _finish(result, started)

            options = _quiz_options(html)
            poster = _QUIZ_POSTER_RE.search(html)
            if not options or not poster:
                result.state = state if state != "unknown" else "unknown"
                result.detail = "页面里没找到答题表单"
                return _finish(result, started)

            image_url = urljoin(str(resp.url), html_lib.unescape(poster.group(1)))
            try:
                image = client.get(image_url).content
            except Exception as exc:  # 取封面失败就换一道题
                missing.append(f"封面下载失败：{exc}")
                continue
            md5 = hashlib.md5(image).hexdigest()
            title = table.get(md5)
            if not title:
                missing.append(md5[:8])
                continue

            picked = _quiz_pick(options, title)
            if picked is None:
                missing.append(f"{title}（选项里没有）")
                continue

            name, value, label = picked
            submit = _QUIZ_SUBMIT_RE.search(html)
            payload = {name: html_lib.unescape(value)}
            if submit:
                payload[submit.group(1)] = submit.group(2) or "提交"
            else:
                payload["submit"] = "提交"
            post = client.post(
                url, data=payload, headers={"Referer": str(resp.url)}
            )
            result.http = post.status_code
            result.debug_html = post.text
            result.final_url = str(post.url)
            text = page_text(post.text)
            st2, why2 = classify(
                post.text,
                final_url=str(post.url),
                success_keywords=success_keywords,
                already_keywords=already_keywords,
            )
            if st2 in ("already", "signed"):
                result.state = st2
                result.detail = why2 or f"签到成功（答题：{label}）"
                return _finish(result, started)
            if any(word in text for word in _QUIZ_WRONG_MARKERS):
                result.state = "failed"
                result.detail = f"答题未通过（提交：{label}）"
                return _finish(result, started)
            result.state = "unknown"
            result.detail = f"提交后没看到签到结果（提交：{label}）"
            return _finish(result, started)

    result.state = "unknown"
    tail = f"（换题 {len(missing)} 次仍没命中：{'、'.join(missing[-3:])}）" if missing else ""
    result.detail = "题库里没有当前封面" + tail
    return _finish(result, started)


def _get_with_retry(
    client: httpx.Client, url: str, *, attempts: int = 4, delay: float = 10.0
) -> httpx.Response:
    """GET 一次，遇到 5xx 或连接错误就等一会儿重试。

    PT 站每天 0 点重置签到，整点那一两分钟经常直接 500；
    这种服务端抖动不该被记成「签到失败」。
    """
    last_error: Exception | None = None
    resp: httpx.Response | None = None
    for index in range(max(1, attempts)):
        try:
            resp = client.get(url)
        except httpx.HTTPError as exc:  # 连接被重置、超时等
            last_error = exc
            resp = None
        else:
            if resp.status_code < 500:
                return resp
            last_error = None
        if index < attempts - 1:
            time.sleep(delay)
    if resp is not None:
        return resp
    raise last_error or RuntimeError("请求失败")


def sign_site(
    *,
    url: str,
    cookie: str = "",
    method: str = "auto",
    sign_url: str = "",
    post_data: str = "",
    headers: str = "",
    success_keywords: str = "",
    already_keywords: str = "",
    timeout: int = 40,
    flaresolverr: str = "",
    quiz_table: str = "",
) -> SignResult:
    """执行一次签到。永不抛异常。"""
    started = time.monotonic()
    today = date.today().isoformat()
    cookies = parse_cookie(cookie)
    req_headers = {"User-Agent": DEFAULT_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    req_headers.update(parse_headers(headers))

    result = SignResult(state="failed", http=0, final_url=url)
    if not url:
        result.detail = "没有填写签到页地址"
        return result
    if not cookies:
        result.detail = "没有填写 Cookie"
        return result

    if method == "quiz":
        return _sign_via_quiz(
            result,
            started,
            url=url,
            cookie=cookie,
            headers=req_headers,
            timeout=timeout,
            success_keywords=success_keywords,
            already_keywords=already_keywords,
            today=today,
            table_path=quiz_table or str(Path("runtime/data/site_checkin_quiz.json")),
        )

    if method == "browser" and flaresolverr:
        return _sign_via_browser(
            result,
            started,
            url=url,
            cookie=cookie,
            endpoint=flaresolverr,
            timeout=timeout,
            success_keywords=success_keywords,
            already_keywords=already_keywords,
            today=today,
        )

    try:
        with netproxy.client_for(url,
            headers=req_headers,
            cookies=cookies,
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            resp = _get_with_retry(client, url)
            html = resp.text or ""
            result.http = resp.status_code
            result.final_url = str(resp.url)

            if _cf_blocked(resp) and flaresolverr:
                solved = _fetch_via_flaresolverr(url, flaresolverr, timeout)
                if solved:
                    html = solved
                    resp = httpx.Response(200, text=solved, request=resp.request)

            state, why = classify(
                html,
                final_url=result.final_url,
                success_keywords=success_keywords,
                already_keywords=already_keywords,
            )
            result.page_text = page_text(html)[:400]
            _stash(result, html)

            if state in ("already", "signed") and not _stale_page(state, result.dates, today):
                result.state = state
                result.detail = why or ("今日已签到" if state == "already" else "签到成功")
                return _finish(result, started)

            if state == "login":
                result.detail = "Cookie 已失效，请重新复制"
                return _finish(result, started)

            if state == "captcha":
                solved = ""
                if flaresolverr:
                    solved = _fetch_via_flaresolverr(url, flaresolverr, timeout)
                if solved:
                    html = solved
                    state, why = classify(
                        html,
                        final_url=result.final_url,
                        success_keywords=success_keywords,
                        already_keywords=already_keywords,
                    )
                    _stash(result, html)
                    result.page_text = page_text(html)[:400]
                    if state in ("already", "signed") and not _stale_page(
                        state, result.dates, today
                    ):
                        result.state = state
                        result.detail = why or "签到成功"
                        return _finish(result, started)
                result.state = "captcha"
                result.detail = "需要 Cloudflare Turnstile 验证（脚本过不去，需要浏览器）"
                return _finish(result, started)

            if state == "slider" and method in ("auto", "slider", "get"):
                problem = _solve_slider(client, html, url)
                if problem and flaresolverr:
                    # 脚本过不了滑块就交给真浏览器（TRAWL）兜底，别直接判失败
                    return _sign_via_browser(
                        result,
                        started,
                        url=url,
                        cookie=cookie,
                        endpoint=flaresolverr,
                        timeout=timeout,
                        success_keywords=success_keywords,
                        already_keywords=already_keywords,
                        today=today,
                    )
                if problem:
                    result.detail = f"滑动认证未通过：{problem}"
                    return _finish(result, started)
                resp = _get_with_retry(client, url)
                html = resp.text or ""
                state, why = classify(
                    html,
                    final_url=str(resp.url),
                    success_keywords=success_keywords,
                    already_keywords=already_keywords,
                )
                result.http = resp.status_code
                result.final_url = str(resp.url)
                _stash(result, html)
                result.page_text = page_text(html)[:400]
                if state in ("already", "signed") and not _stale_page(state, result.dates, today):
                    result.state = state
                    result.detail = why or "签到成功"
                    return _finish(result, started)
                if state == "slider":
                    result.detail = "滑动认证未通过（token 未被服务端接受，需浏览器）"
                    return _finish(result, started)

            if method != "get" and (_looks_like_sign_form(html) or sign_url):
                if "attendanceretroactive" in (post_data or "").lower():
                    # 补签接口会消耗补签卡，只该用于漏签的日期；今日签到不能走这里
                    result.state = "failed"
                    result.detail = "配置里的签到动作是补签接口（attendanceRetroactive），已跳过，避免消耗补签卡"
                    return _finish(result, started)
                parsed = _form_fields(html)
                action = sign_url or (urljoin(url, parsed[0]) if parsed and parsed[0] else url)
                form_method = (parsed[1] if parsed else "post").upper()
                fields = dict(parsed[2]) if parsed else {}
                for name, value in parse_pairs(post_data).items():
                    fields[name] = str(value).replace("{date}", today).replace("{today}", today)
                post_headers = dict(req_headers)
                if sign_url and "ajax" in sign_url:
                    post_headers["X-Requested-With"] = "XMLHttpRequest"
                    post_headers["Referer"] = url
                if form_method == "GET":
                    resp = client.get(action, params=fields, headers=post_headers)
                else:
                    resp = client.post(action, data=fields, headers=post_headers)
                html = resp.text or ""
                result.http = resp.status_code
                result.final_url = str(resp.url)
                _stash(result, html)
                result.page_text = page_text(html)[:400]
                json_state = _json_state(html)
                if json_state:
                    result.state, detail = json_state
                    result.detail = detail
                    if result.state == "failed":
                        result.detail = f"站点返回失败：{detail}"
                    return _finish(result, started)
                state, why = classify(
                    html,
                    final_url=str(resp.url),
                    success_keywords=success_keywords,
                    already_keywords=already_keywords,
                )
                if state in ("signed", "already") and not _stale_page(state, result.dates, today):
                    result.state = state
                    result.detail = why or "签到成功"
                    return _finish(result, started)
                if state == "login":
                    result.detail = "提交后跳到登录页，Cookie 可能已失效"
                    return _finish(result, started)

            if result.http >= 500:
                result.state = "failed"
                result.detail = (
                    f"站点返回 HTTP {result.http}（0 点重置高峰常出现，稍后会自动再试）"
                )
                return _finish(result, started)
            result.detail = f"页面没有找到签到入口（HTTP {result.http}）"
            return _finish(result, started)
    except Exception as exc:  # noqa: BLE001
        result.detail = f"{type(exc).__name__}: {exc}"[:200]
        return _finish(result, started)


def _finish(result: SignResult, started: float) -> SignResult:
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    _decorate(result)
    return result


_BONUS_RE = re.compile(
    r"签到已得\s*([\d,]+(?:\.\d+)?)"
    r"|本次签到获得\s*([\d,]+(?:\.\d+)?)\s*个?\s*魔力"
    r"|获得\s*([\d,]+(?:\.\d+)?)\s*个\s*魔力"
)


def _bonus_amount(html: str) -> str:
    """从页面里抠出「今天签到得了多少魔力」（不同站点写法不同）。"""
    match = _BONUS_RE.search(page_text(html) or "")
    if not match:
        match = _BONUS_RE.search(html or "")
    if not match:
        return ""
    for group in match.groups():
        if group:
            return group
    return ""


def _decorate(result: SignResult) -> None:
    """把「获得 N 个魔力值」补进说明里；already 时替掉「请勿重复刷新」那种二次签到文案。"""
    bonus = _bonus_amount(result.debug_html or result.page_text or "")
    if not bonus:
        return
    result.bonus = bonus
    detail = (result.detail or "").strip()
    if result.state == "already":
        result.detail = f"今日已签到 · 本次获得 {bonus} 个魔力值"
    elif result.state == "signed":
        if "魔力" not in detail:
            result.detail = (detail or "签到成功").rstrip("。，") + f"，获得 {bonus} 个魔力值"


def result_json(result: SignResult) -> str:
    return json.dumps(
        {
            "state": result.state,
            "detail": result.detail,
            "http": result.http,
            "elapsed_ms": result.elapsed_ms,
            "final_url": result.final_url,
            "bonus": result.bonus,
        },
        ensure_ascii=False,
    )


def as_dict(result: SignResult) -> dict[str, Any]:
    return {
        "state": result.state,
        "detail": result.detail,
        "http": result.http,
        "elapsed_ms": result.elapsed_ms,
        "final_url": result.final_url,
        "page_text": result.page_text,
        "bonus": result.bonus,
    }
