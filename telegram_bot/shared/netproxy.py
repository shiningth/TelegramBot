"""出网代理：统一决定「这条请求走不走代理」。

设计要点：
- 透明代理/直连环境什么都不用配（`enabled=False` 时全部直连）。
- 局域网必须绕过：momo、FlareSolverr、TRAWL 都在内网，且 momo 的 DB/IP 是私网地址，
  所以私网 IP、localhost、`.local/.lan` 一律直连，不受 `no_proxy` 影响。
- httpx 自己会读 `HTTP_PROXY` 环境变量，但 Telethon 不会——所以 Telegram 侧
  必须由 `telethon_proxy()` 显式返回三元组，交给 `TelegramClient(proxy=...)`。
"""

from __future__ import annotations

import ipaddress
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

_CONFIG: Any = None

_SCHEME_DEFAULT_PORT = {"http": 3128, "https": 3128, "socks5": 1080, "socks5h": 1080, "socks4": 1080}


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
    auth = f"{spec['username']}:***@" if spec["username"] else ""
    return f"{spec['scheme']}://{auth}{spec['host']}:{spec['port']}"


def _host_of(target: str) -> str:
    raw = (target or "").strip()
    if "://" not in raw:
        return raw.split("/")[0].split(":")[0].lower()
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


def client_for(target: str, *, for_web: bool = True, **kwargs: Any) -> httpx.Client:
    """按目标地址决定要不要代理，返回一个 httpx.Client（调用方用 with 收尾）。"""
    cfg = _CONFIG
    if cfg is not None and _active() and getattr(cfg, "proxy_for_web", True) is not False:
        if for_web and not should_bypass(target):
            spec = parse_proxy_url(getattr(cfg, "proxy_url", ""))
            if spec:
                kwargs.setdefault("proxy", spec["url"])
    return httpx.Client(**kwargs)


def client_external(**kwargs: Any) -> httpx.Client:
    """已知目标是公网的场景（DMM/Jav321/JavDB 等）：代理开着就一律走代理。"""
    cfg = _CONFIG
    if cfg is not None and _active() and getattr(cfg, "proxy_for_web", True) is not False:
        spec = parse_proxy_url(getattr(cfg, "proxy_url", ""))
        if spec:
            kwargs.setdefault("proxy", spec["url"])
    return httpx.Client(**kwargs)


def telethon_proxy() -> Any:
    """给 TelegramClient(proxy=...) 用；无代理或 socks 依赖缺失时返回 None（直连）。"""
    cfg = _CONFIG
    if cfg is None or not _active() or not getattr(cfg, "proxy_for_telegram", True):
        return None
    spec = parse_proxy_url(getattr(cfg, "proxy_url", ""))
    if not spec:
        return None
    try:
        from python_socks import ProxyType  # type: ignore
    except Exception:
        return None
    kinds = {
        "http": ProxyType.HTTP,
        "https": ProxyType.HTTP,
        "socks4": ProxyType.SOCKS4,
        "socks5": ProxyType.SOCKS5,
        "socks5h": ProxyType.SOCKS5,
    }
    kind = kinds.get(spec["scheme"])
    if kind is None:
        return None
    return (
        kind,
        spec["host"],
        spec["port"],
        True,
        spec["username"] or None,
        spec["password"] or None,
    )


def direct_ip(*, timeout: float = 8.0) -> str:
    """不经代理探一次出口 IP，用来和代理出口做对照。"""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get("https://ipinfo.io/json")
        if resp.status_code == 200:
            return str(resp.json().get("ip") or "")
    except Exception:  # noqa: BLE001
        return ""
    return ""


def test_proxy(proxy_url: str, *, timeout: float = 12.0, compare_direct: bool = True) -> dict[str, Any]:
    """面板「测试代理」：直接经代理访问一个回显 IP 的服务。"""
    spec = parse_proxy_url(proxy_url)
    if not spec:
        return {"ok": False, "error": "代理地址格式不对，例：http://127.0.0.1:7890 或 socks5://user:pass@host:1080"}
    started = time.monotonic()
    detail: dict[str, Any] = {"proxy": mask_proxy_url(proxy_url)}
    try:
        with httpx.Client(proxy=spec["url"], timeout=timeout, follow_redirects=True) as client:
            resp = client.get("https://ipinfo.io/json")
        elapsed = int((time.monotonic() - started) * 1000)
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code}", "elapsed_ms": elapsed, **detail}
        data = resp.json()
        result = {
            "ok": True,
            "ip": data.get("ip", ""),
            "country": data.get("country", ""),
            "city": data.get("city", ""),
            "org": (data.get("org") or "").split(" ")[0],
            "elapsed_ms": elapsed,
            **detail,
        }
        if compare_direct:
            plain = direct_ip()
            result["direct_ip"] = plain
            result["egress_changed"] = bool(plain and result["ip"] and plain != result["ip"])
        return result
    except Exception as exc:  # noqa: BLE001 - 面板要把原因显示给人看
        elapsed = int((time.monotonic() - started) * 1000)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "elapsed_ms": elapsed, **detail}
