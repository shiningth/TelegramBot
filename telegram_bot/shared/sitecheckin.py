"""网页签到通用实现：用 Cookie 直接请求站点签到页/接口。

设计原则：
1. 站点无关——所有差异都走配置（url / method / sign_url / headers / 关键字）。
2. 先直连，只有明确被 Cloudflare 拦住才回落 FlareSolverr。
3. 任何一步失败都返回可读原因，不抛异常给调度器。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urljoin

import httpx

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
    return re.sub(r"\s+", " ", body).strip()


def _keywords(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,，\n]", str(text or "")) if item.strip()]


def _snippet(text: str, word: str, width: int = 48) -> str:
    """把命中关键字附近的一小段文字带回去，日志里能看出站点到底说了什么。"""
    index = text.find(word) if word else -1
    if index < 0:
        return word
    return text[index : index + width].strip()


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


def _solve_slider(client: httpx.Client, html: str, base_url: str) -> str:
    """NexusPHP 系滑动认证：取 slide_check JS 里的 set_access_token 地址并请求。

    这一步能让「必须拖到底」的验证在服务端通过；站点若把 token 绑在
    真实浏览器会话上，这里会失败并如实返回，交给浏览器兜底。
    """
    match = _SLIDER_JS_RE.search(html or "")
    if not match:
        return ""
    js_url = urljoin(base_url, match.group(1))
    try:
        js = client.get(js_url).text
    except Exception as exc:  # noqa: BLE001
        return f"取滑块脚本失败：{type(exc).__name__}"
    token = _TOKEN_URL_RE.search(js or "")
    if not token:
        return "滑块脚本里没有找到验证地址"
    try:
        client.get(token.group(0), headers={"X-Requested-With": "XMLHttpRequest", "Accept": "*/*"})
    except Exception as exc:  # noqa: BLE001
        return f"提交滑块验证失败：{type(exc).__name__}"
    return ""


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

    try:
        with httpx.Client(
            headers=req_headers,
            cookies=cookies,
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            resp = client.get(url)
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

            if state == "slider" and method in ("auto", "slider"):
                problem = _solve_slider(client, html, url)
                if problem:
                    result.detail = f"滑动认证未通过：{problem}"
                    return _finish(result, started)
                resp = client.get(url)
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

            result.detail = f"页面没有找到签到入口（HTTP {result.http}）"
            return _finish(result, started)
    except Exception as exc:  # noqa: BLE001
        result.detail = f"{type(exc).__name__}: {exc}"[:200]
        return _finish(result, started)


def _finish(result: SignResult, started: float) -> SignResult:
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result


def result_json(result: SignResult) -> str:
    return json.dumps(
        {
            "state": result.state,
            "detail": result.detail,
            "http": result.http,
            "elapsed_ms": result.elapsed_ms,
            "final_url": result.final_url,
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
    }
