"""出网代理：统一决定「这条请求走不走代理」。

设计要点：
- 透明代理/直连环境什么都不用配（`enabled=False` 时全部直连）。
- 局域网必须绕过：momo、FlareSolverr、TRAWL 都在内网，且 momo 的 DB/IP 是私网地址，
  所以私网 IP、localhost、`.local/.lan` 一律直连，不受 `no_proxy` 影响。
- HTTP 与 Telegram 客户端都使用面板设置决定路由，直连不会继承 `HTTP_PROXY`。
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

_CONFIG: Any = None

_SCHEME_DEFAULT_PORT = {"http": 3128, "https": 3128, "socks5": 1080, "socks5h": 1080, "socks4": 1080}
_URL_CREDENTIALS_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@")


def _redact_proxy_urls(text: str) -> str:
    return _URL_CREDENTIALS_RE.sub(r"\1***:***@", str(text or ""))


def configure(config: Any) -> None:
    """App 启动和面板保存后调用；传 None 表示全部直连（测试里也用）。"""
    global _CONFIG
    _CONFIG = config


def config() -> Any:
    return _CONFIG


def _active() -> bool:
    cfg = _CONFIG
    if cfg is None:
        return False
    try:
        return bool(cfg.active())
    except AttributeError:
        return bool(getattr(cfg, "enabled", False) and str(getattr(cfg, "proxy_url", "")).strip())


def parse_proxy_url(proxy_url: str) -> dict[str, Any] | None:
    raw = (proxy_url or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    scheme = (parts.scheme or "http").lower()
    if scheme not in _SCHEME_DEFAULT_PORT:
        return None
    return {
        "scheme": scheme,
        "host": host,
        "port": port or _SCHEME_DEFAULT_PORT.get(scheme, 3128),
        "username": parts.username or "",
        "password": parts.password or "",
        "url": raw,
    }


def mask_proxy_url(proxy_url: str) -> str:
    spec = parse_proxy_url(proxy_url)
    if not spec:
        return ""
    auth = "***@" if spec["username"] or spec["password"] else ""
    host = spec["host"]
    if ":" in host:
        host = f"[{host}]"
    return f"{spec['scheme']}://{auth}{host}:{spec['port']}"


def _host_of(target: str) -> str:
    raw = (target or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        candidate = raw.split("/")[0].strip()
        bare = candidate.strip("[]")
        try:
            ipaddress.ip_address(bare)
            return bare.lower()
        except ValueError:
            pass
        if candidate.startswith("["):
            try:
                return (urlsplit("http://" + candidate).hostname or "").lower()
            except ValueError:
                return ""
        return candidate.split(":")[0].lower()
    try:
        return (urlsplit(raw).hostname or "").lower()
    except ValueError:
        return ""


def _is_private_host(host: str) -> bool:
    if not host:
        return True
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    if host.endswith(".local") or host.endswith(".lan") or "." not in host:
        # 无点主机名多为 docker 内部服务名（momo、flaresolverr…），走直连
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


def _pattern_matches(host: str, pattern: str) -> bool:
    pat = (pattern or "").strip().lower()
    if not pat:
        return False
    if "/" in pat:
        try:
            return ipaddress.ip_address(host) in ipaddress.ip_network(pat, strict=False)
        except ValueError:
            return False
    if pat.startswith("*."):
        pat = pat[1:]
    if pat.startswith("."):
        return host.endswith(pat) or host == pat[1:]
    return host == pat or host.endswith("." + pat)


def should_bypass(target: str) -> bool:
    """返回 True 表示这条请求直连（不代理）。"""
    if not _active():
        return True
    host = _host_of(target)
    if _is_private_host(host):
        return True
    for pattern in getattr(_CONFIG, "no_proxy", []) or []:
        if _pattern_matches(host, pattern):
            return True
    return False


def proxy_url_for(target: str, *, for_web: bool = True) -> str | None:
    cfg = _CONFIG
    if cfg is not None and _active() and getattr(cfg, "proxy_for_web", True) is not False:
        if for_web and not should_bypass(target):
            spec = parse_proxy_url(getattr(cfg, "proxy_url", ""))
            if spec:
                return spec["url"]
    return None


def client_for(target: str, *, for_web: bool = True, **kwargs: Any) -> httpx.Client:
    """按目标地址决定要不要代理，返回一个 httpx.Client（调用方用 with 收尾）。"""
    proxy_url = proxy_url_for(target, for_web=for_web)
    if proxy_url:
        kwargs.setdefault("proxy", proxy_url)
    return httpx.Client(trust_env=False, **kwargs)


def client_external(**kwargs: Any) -> httpx.Client:
    """已知目标是公网的场景（DMM/Jav321/JavDB 等）：代理开着就一律走代理。"""
    cfg = _CONFIG
    if cfg is not None and _active() and getattr(cfg, "proxy_for_web", True) is not False:
        spec = parse_proxy_url(getattr(cfg, "proxy_url", ""))
        if spec:
            kwargs.setdefault("proxy", spec["url"])
    return httpx.Client(trust_env=False, **kwargs)


def client_telegram(**kwargs: Any) -> httpx.Client:
    """Telegram Bot API（api.telegram.org）专用：受 proxy_for_telegram 控制。"""
    cfg = _CONFIG
    if cfg is not None and _active() and getattr(cfg, "proxy_for_telegram", True):
        if not should_bypass("https://api.telegram.org"):
            spec = parse_proxy_url(getattr(cfg, "proxy_url", ""))
            if spec:
                kwargs.setdefault("proxy", spec["url"])
    return httpx.Client(trust_env=False, **kwargs)


def telegram_proxy_route(cfg: Any) -> tuple[str, str, int, str, str] | None:
    if cfg is None:
        return None
    try:
        is_active = bool(cfg.active())
    except AttributeError:
        is_active = bool(getattr(cfg, "enabled", False) and str(getattr(cfg, "proxy_url", "")).strip())
    if not is_active or not getattr(cfg, "proxy_for_telegram", True):
        return None
    spec = parse_proxy_url(getattr(cfg, "proxy_url", ""))
    if not spec or spec["scheme"] not in _SCHEME_DEFAULT_PORT:
        return None
    return spec["scheme"], spec["host"], spec["port"], spec["username"], spec["password"]


def telethon_proxy() -> Any:
    """给 TelegramClient(proxy=...) 用；无代理或 socks 依赖缺失时返回 None（直连）。"""
    route = telegram_proxy_route(_CONFIG)
    if route is None:
        return None
    scheme, host, port, username, password = route
    try:
        from python_socks import ProxyType  # type: ignore
    except Exception:
        logger.warning("python-socks is unavailable; Telethon proxy %s://%s:%s bypassed", scheme, host, port)
        return None
    kinds = {
        "http": ProxyType.HTTP,
        "https": ProxyType.HTTP,
        "socks4": ProxyType.SOCKS4,
        "socks5": ProxyType.SOCKS5,
        "socks5h": ProxyType.SOCKS5,
    }
    return kinds[scheme], host, port, True, username or None, password or None


_IP_PROBES: tuple[tuple[str, str], ...] = (
    ("cloudflare", "https://www.cloudflare.com/cdn-cgi/trace"),
    ("ip.sb", "https://api.ip.sb/geoip"),
    ("ipinfo", "https://ipinfo.io/json"),
    ("ipify", "https://api.ipify.org?format=json"),
)

_CONNECTIVITY_PROBES: tuple[tuple[str, str], ...] = (
    ("Google 204", "https://www.gstatic.com/generate_204"),
    ("GitHub", "https://github.com/robots.txt"),
    ("Telegram API", "https://api.telegram.org"),
)


def _parse_ip_response(kind: str, resp: Any) -> dict[str, str] | None:
    if kind == "cloudflare":
        text = getattr(resp, "text", "")
        if isinstance(text, str) and "ip=" in text:
            fields: dict[str, str] = {}
            for line in text.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    fields[k.strip()] = v.strip()
            ip = fields.get("ip", "")
            if ip:
                return {"ip": ip, "country": fields.get("loc", ""), "city": "", "org": ""}
        return None
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    ip = str(data.get("ip") or "").strip()
    if not ip:
        return None
    country = str(data.get("country_code") or data.get("country") or "").strip()
    city = str(data.get("city") or "").strip()
    org_raw = str(data.get("org") or data.get("organization") or data.get("asn_organization") or "").strip()
    org = org_raw.split(" ")[0] if kind == "ipinfo" else org_raw
    return {"ip": ip, "country": country, "city": city, "org": org}


def _probe_ip(client: httpx.Client) -> tuple[dict[str, str] | None, str]:
    last_error = ""
    for kind, url in _IP_PROBES:
        try:
            resp = client.get(url)
        except Exception as exc:  # noqa: BLE001
            last_error = _redact_proxy_urls(f"{type(exc).__name__}: {exc}")[:200]
            continue
        if resp.status_code != 200:
            last_error = f"{kind} HTTP {resp.status_code}"
            continue
        parsed = _parse_ip_response(kind, resp)
        if parsed:
            parsed["probe"] = kind
            return parsed, ""
        last_error = f"{kind} 返回格式异常"
    return None, last_error or "无法获取出口 IP"


def _probe_connectivity(client: httpx.Client) -> tuple[str, str]:
    """当所有查 IP 源都被限流（如 429）时，用大厂探针确认代理本身是否连通。"""
    last_error = ""
    for name, url in _CONNECTIVITY_PROBES:
        try:
            resp = client.get(url)
        except Exception as exc:  # noqa: BLE001
            last_error = _redact_proxy_urls(f"{type(exc).__name__}: {exc}")[:200]
            continue
        if resp.status_code < 500:
            return name, ""
        last_error = f"{name} HTTP {resp.status_code}"
    return "", last_error


def direct_ip(*, timeout: float = 8.0) -> str:
    """不经代理探一次出口 IP，用来和代理出口做对照。"""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False) as client:
            parsed, _ = _probe_ip(client)
        return parsed["ip"] if parsed else ""
    except Exception:  # noqa: BLE001
        return ""


def test_proxy(proxy_url: str, *, timeout: float = 12.0, compare_direct: bool = True) -> dict[str, Any]:
    """面板「测试代理」：多源探测出口 IP，全被限流时用 Google/GitHub/Telegram 兜底验证连通性。"""
    spec = parse_proxy_url(proxy_url)
    if not spec:
        return {"ok": False, "error": "代理地址格式不对，例：http://127.0.0.1:7890 或 socks5://user:pass@host:1080"}
    started = time.monotonic()
    detail: dict[str, Any] = {"proxy": mask_proxy_url(proxy_url)}
    try:
        with httpx.Client(proxy=spec["url"], timeout=timeout, follow_redirects=True, trust_env=False) as client:
            parsed, ip_err = _probe_ip(client)
            if parsed:
                elapsed = int((time.monotonic() - started) * 1000)
                result: dict[str, Any] = {
                    "ok": True,
                    "ip": parsed["ip"],
                    "country": parsed["country"],
                    "city": parsed["city"],
                    "org": parsed["org"],
                    "probe": parsed["probe"],
                    "elapsed_ms": elapsed,
                    **detail,
                }
                if compare_direct:
                    plain = direct_ip()
                    result["direct_ip"] = plain
                    result["egress_changed"] = bool(plain and result["ip"] and plain != result["ip"])
                return result
            conn_probe, conn_err = _probe_connectivity(client)
            elapsed = int((time.monotonic() - started) * 1000)
            if conn_probe:
                return {
                    "ok": True,
                    "ip": "",
                    "country": "",
                    "city": "",
                    "org": "",
                    "probe": conn_probe,
                    "note": f"已通过 {conn_probe} 连通性验证（查 IP 接口暂不可用或被限流）",
                    "elapsed_ms": elapsed,
                    **detail,
                }
            return {"ok": False, "error": conn_err or ip_err, "elapsed_ms": elapsed, **detail}
    except Exception as exc:  # noqa: BLE001 - 面板要把原因显示给人看
        elapsed = int((time.monotonic() - started) * 1000)
        return {"ok": False, "error": _redact_proxy_urls(f"{type(exc).__name__}: {exc}")[:200], "elapsed_ms": elapsed, **detail}
