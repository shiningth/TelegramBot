from __future__ import annotations

import base64
import binascii
import html
import hmac
import json
import logging
import mimetypes
import secrets
import threading
import urllib.parse
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from telegram_bot.logging_setup import list_log_files, read_log_tail

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "telegrambot_admin_session"


def _admin_auth_enabled(admin: Any) -> bool:
    return bool(str(getattr(admin, "username", "") or "")) and bool(str(getattr(admin, "password", "") or ""))


def _credentials_ok(username: str, password: str, expected_username: str, expected_password: str) -> bool:
    return hmac.compare_digest(str(username), str(expected_username)) and hmac.compare_digest(str(password), str(expected_password))


def _basic_auth_ok(header: str, username: str, password: str) -> bool:
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False
    user, sep, pwd = decoded.partition(":")
    if not sep:
        return False
    return _credentials_ok(user, pwd, username, password)


def _cookie_value(header: str, name: str) -> str:
    if not header:
        return ""
    cookie = SimpleCookie()
    try:
        cookie.load(header)
    except Exception:
        return ""
    morsel = cookie.get(name)
    return morsel.value if morsel is not None else ""


def _session_auth_ok(cookie_header: str, active_tokens: set[str]) -> bool:
    token = _cookie_value(cookie_header, SESSION_COOKIE_NAME)
    return bool(token and token in active_tokens)


def _new_session_token() -> str:
    return secrets.token_urlsafe(32)


def _session_cookie(token: str) -> str:
    return f"{SESSION_COOKIE_NAME}={token}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax"


def _clear_session_cookie() -> str:
    return f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"


class AdminServer:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.session_tokens: set[str] = set()

    def start(self) -> None:
        handler = self._handler_class()
        host = self.app.config.admin.host
        port = int(self.app.config.admin.port)
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="admin-http", daemon=True)
        self.thread.start()
        if _admin_auth_enabled(self.app.config.admin):
            logger.info("Admin UI listening on http://%s:%s (Basic Auth enabled)", host, port)
        else:
            logger.warning(
                "Admin UI listening on http://%s:%s WITHOUT auth; set admin.username/password "
                "(or TGBOT_ADMIN_USERNAME/PASSWORD) to protect it.",
                host,
                port,
            )

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        logger.info("Admin UI stopped")

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        app = self.app
        static_dir = Path(__file__).resolve().parent / "static"
        session_tokens = self.session_tokens

        class Handler(BaseHTTPRequestHandler):
            server_version = "TelegramBotAdmin/0.1"

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/login":
                    self._serve_login(parsed)
                    return
                if parsed.path == "/api/auth":
                    self._json({"authenticated": self._is_authenticated(), "auth_enabled": _admin_auth_enabled(app.config.admin)})
                    return
                if not self._require_auth(parsed.path):
                    return
                if parsed.path.startswith("/api/"):
                    self._handle_api_get(parsed)
                    return
                self._serve_static(parsed.path, static_dir)

            def do_POST(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/api/login":
                    self._handle_login()
                    return
                if parsed.path == "/api/logout":
                    self._handle_logout()
                    return
                if not self._require_auth(parsed.path):
                    return
                if parsed.path.startswith("/api/"):
                    self._handle_api_post(parsed)
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def do_PUT(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                if not self._require_auth(parsed.path):
                    return
                if parsed.path.startswith("/api/"):
                    self._handle_api_put(parsed)
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def _require_auth(self, path: str) -> bool:
                if not _admin_auth_enabled(app.config.admin):
                    return True
                if self._is_authenticated():
                    return True
                if _basic_auth_ok(
                    self.headers.get("Authorization", ""),
                    app.config.admin.username,
                    app.config.admin.password,
                ):
                    return True
                if self.command == "GET" and not path.startswith("/api/"):
                    self._redirect(f"/login?next={urllib.parse.quote(self.path or '/', safe='')}")
                    return False
                self._json({"error": "login required"}, HTTPStatus.UNAUTHORIZED)
                return False

            def _is_authenticated(self) -> bool:
                if not _admin_auth_enabled(app.config.admin):
                    return True
                return _session_auth_ok(self.headers.get("Cookie", ""), session_tokens)

            def _handle_login(self) -> None:
                if not _admin_auth_enabled(app.config.admin):
                    self._json({"ok": True, "auth_enabled": False})
                    return
                body = self._body_json()
                username = str(body.get("username") or "")
                password = str(body.get("password") or "")
                if not _credentials_ok(username, password, app.config.admin.username, app.config.admin.password):
                    self._json({"ok": False, "error": "invalid credentials"}, HTTPStatus.UNAUTHORIZED)
                    return
                token = _new_session_token()
                session_tokens.add(token)
                self._json({"ok": True}, headers={"Set-Cookie": _session_cookie(token)})

            def _handle_logout(self) -> None:
                token = _cookie_value(self.headers.get("Cookie", ""), SESSION_COOKIE_NAME)
                if token:
                    session_tokens.discard(token)
                self._json({"ok": True}, headers={"Set-Cookie": _clear_session_cookie()})

            def _serve_login(self, parsed: urllib.parse.ParseResult) -> None:
                if self._is_authenticated():
                    self._redirect("/")
                    return
                query = urllib.parse.parse_qs(parsed.query)
                next_url = query.get("next", ["/"])[0]
                if not next_url.startswith("/") or next_url.startswith("//"):
                    next_url = "/"
                data = _login_page(next_url).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _redirect(self, target: str) -> None:
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, fmt: str, *args: Any) -> None:
                logger.debug("admin-http " + fmt, *args)

            def _handle_api_get(self, parsed: urllib.parse.ParseResult) -> None:
                query = urllib.parse.parse_qs(parsed.query)
                if parsed.path == "/api/status":
                    self._json(app.status_snapshot())
                elif parsed.path == "/api/modules":
                    self._json({"modules": app.modules.cards()})
                elif parsed.path.startswith("/api/modules/"):
                    module_id = parsed.path.rsplit("/", 1)[-1]
                    module = app.modules.get(module_id)
                    if not module:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    self._json(module.card.__dict__)
                elif parsed.path == "/api/ai":
                    self._json(app.ai_snapshot())
                elif parsed.path == "/api/ai/usage":
                    range_key = query.get("range", ["today"])[0]
                    self._json(app.ai_usage_snapshot(range_key))
                elif parsed.path == "/api/settings":
                    self._json(app.settings_snapshot())
                elif parsed.path == "/api/groups":
                    try:
                        self._json({"groups": app.list_groups()})
                    except RuntimeError as exc:
                        logger.warning("Cannot list Telegram groups: %s", exc)
                        self._json({"groups": [], "error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
                    except Exception as exc:
                        logger.exception("Failed to list Telegram groups")
                        self._json({"groups": [], "error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
                elif parsed.path == "/api/telegram-targets":
                    try:
                        self._json({"targets": app.list_targets()})
                    except RuntimeError as exc:
                        logger.warning("Cannot list Telegram targets: %s", exc)
                        self._json({"targets": [], "error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
                    except Exception as exc:
                        logger.exception("Failed to list Telegram targets")
                        self._json({"targets": [], "error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
                elif parsed.path == "/api/logs/recent":
                    self._json({"records": app.recent_logs.tail(200)})
                elif parsed.path == "/api/logs/files":
                    self._json(
                        {
                            "files": list_log_files(app.config.logs, app.project_root),
                            "keep_days": app.config.logs.keep_days,
                            "max_files": app.config.logs.max_files,
                        }
                    )
                elif parsed.path == "/api/logs/read":
                    name = query.get("file", [app.config.logs.file_name])[0]
                    lines = int(query.get("lines", ["300"])[0])
                    try:
                        self._json({"name": name, "content": read_log_tail(app.config.logs, app.project_root, name, lines)})
                    except (FileNotFoundError, ValueError):
                        self.send_error(HTTPStatus.NOT_FOUND)
                elif parsed.path == "/api/logs/download":
                    name = query.get("file", [app.config.logs.file_name])[0]
                    self._download_log(name)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)

            def _handle_api_post(self, parsed: urllib.parse.ParseResult) -> None:
                if parsed.path == "/api/service/start":
                    app.request_service_action("start")
                    self._json({"ok": True, "action": "start"})
                elif parsed.path == "/api/service/stop":
                    app.request_service_action("stop")
                    self._json({"ok": True, "action": "stop"})
                elif parsed.path == "/api/service/restart":
                    app.request_service_action("restart")
                    self._json({"ok": True, "action": "restart"})
                elif parsed.path == "/api/modules/ltx_watch/notify-test":
                    result = app.run_module_action("ltx_watch", "notify_test")
                    self._json(result, HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
                elif parsed.path == "/api/modules/site_checkin/run":
                    result = app.run_module_action("site_checkin", "run_now", body=self._body_json())
                    self._json(result, HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)

            def _handle_api_put(self, parsed: urllib.parse.ParseResult) -> None:
                body = self._body_json()
                if parsed.path == "/api/ai":
                    self._json({"ok": True, "ai": app.update_ai_config(body)})
                elif parsed.path == "/api/settings":
                    app.update_settings(body)
                    self._json({"ok": True, "settings": app.settings_snapshot()})
                elif parsed.path.startswith("/api/modules/"):
                    module_id = parsed.path.rsplit("/", 1)[-1]
                    try:
                        updated = app.update_module(module_id, body)
                    except ValueError as exc:
                        self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                        return
                    if updated is None:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    self._json({"ok": True, "module": updated})
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)

            def _serve_static(self, path: str, static_dir: Path) -> None:
                relative = "index.html" if path in {"", "/"} else path.lstrip("/")
                target = (static_dir / relative).resolve()
                if static_dir.resolve() not in target.parents and target != static_dir.resolve():
                    self.send_error(HTTPStatus.FORBIDDEN)
                    return
                if not target.exists() or not target.is_file():
                    target = static_dir / "index.html"
                if target.suffix == ".webmanifest":
                    content_type = "application/manifest+json"
                else:
                    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                data = target.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                # 面板会随镜像更新，静态文件一律不缓存，避免升级后浏览器还在用旧的 index.html/app.js
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.end_headers()
                self.wfile.write(data)

            def _download_log(self, name: str) -> None:
                log_dir = (app.project_root / app.config.logs.dir).resolve()
                target = (log_dir / name).resolve()
                if log_dir not in target.parents or not target.exists():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                data = target.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                if not length:
                    return {}
                raw = self.rfile.read(length).decode("utf-8")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    return {}
                return data if isinstance(data, dict) else {}

            def _json(self, data: Any, status: HTTPStatus = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
                payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store, max-age=0")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)

        return Handler


def _login_page(next_url: str) -> str:
    safe_next = html.escape(next_url, quote=True)
    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>TelegramBot 登录</title>
    <style>
      :root {{
        color-scheme: light;
        --ink: #1a1e21;
        --ink-2: #596169;
        --ink-3: #8b9299;
        --paper: #f4f5f6;
        --surface: #ffffff;
        --hair: rgba(20, 24, 28, 0.10);
        --hair-strong: rgba(20, 24, 28, 0.18);
        --amber: #b06a12;
        --err: #a8323f;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        color: var(--ink);
        font-family: "Aptos", "Segoe UI", "Microsoft YaHei", sans-serif;
        background: var(--paper);
      }}
      .login-panel {{
        width: min(392px, calc(100vw - 32px));
        padding: 32px 32px 28px;
        border: 1px solid var(--hair);
        border-radius: 14px;
        background: var(--surface);
        box-shadow: 0 1px 2px rgba(20, 24, 28, 0.04), 0 12px 32px rgba(20, 24, 28, 0.06);
      }}
      .login-brand {{
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 22px;
        color: var(--ink-2);
        font-size: 12px;
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }}
      .login-brand::before {{
        content: "";
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: var(--amber);
      }}
      h1 {{ margin: 0 0 6px; font-size: 19px; font-weight: 600; letter-spacing: -0.01em; }}
      p {{ margin: 0 0 24px; color: var(--ink-3); font-size: 12.5px; line-height: 1.6; }}
      form {{ display: grid; gap: 14px; }}
      label {{ display: grid; gap: 6px; color: var(--ink-2); font-size: 12px; font-weight: 500; }}
      input {{
        width: 100%;
        min-height: 38px;
        border: 1px solid var(--hair-strong);
        border-radius: 8px;
        padding: 8px 11px;
        color: var(--ink);
        background: var(--surface);
        font: inherit;
        font-size: 13.5px;
        transition: border-color 120ms ease, box-shadow 120ms ease;
      }}
      input:focus {{
        outline: none;
        border-color: var(--amber);
        box-shadow: 0 0 0 3px rgba(176, 106, 18, 0.10);
      }}
      button {{
        min-height: 38px;
        margin-top: 4px;
        border: 1px solid var(--amber);
        border-radius: 8px;
        color: #fff;
        background: var(--amber);
        cursor: pointer;
        font: inherit;
        font-size: 13.5px;
        font-weight: 500;
        transition: background 120ms ease;
      }}
      button:hover {{ background: #9d5c0f; }}
      button:disabled {{ opacity: 0.6; cursor: default; }}
      .error {{ min-height: 18px; margin-top: 2px; color: var(--err); font-size: 12.5px; }}
    </style>
  </head>
  <body>
    <main class="login-panel">
      <div class="login-brand">TelegramBot</div>
      <h1>登录控制台</h1>
      <p>登录状态保存在当前服务进程内，服务重启后需要重新登录。</p>
      <form id="loginForm">
        <label>用户名<input name="username" autocomplete="username" required autofocus /></label>
        <label>密码<input name="password" type="password" autocomplete="current-password" required /></label>
        <button type="submit">登录后台</button>
        <div id="loginError" class="error" role="alert"></div>
      </form>
    </main>
    <script>
      const nextUrl = "{safe_next}";
      document.getElementById("loginForm").addEventListener("submit", async (event) => {{
        event.preventDefault();
        const form = event.currentTarget;
        const error = document.getElementById("loginError");
        error.textContent = "";
        const button = form.querySelector("button");
        button.disabled = true;
        try {{
          const response = await fetch("/api/login", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{
              username: form.elements.username.value,
              password: form.elements.password.value,
            }}),
          }});
          if (!response.ok) throw new Error("用户名或密码不正确");
          window.location.assign(nextUrl || "/");
        }} catch (err) {{
          error.textContent = err.message || "登录失败";
        }} finally {{
          button.disabled = false;
        }}
      }});
    </script>
  </body>
</html>"""
