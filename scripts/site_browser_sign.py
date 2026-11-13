#!/usr/bin/env python3
"""用真实 Chromium 过 Cloudflare Turnstile 的站点签到。

OurBits 这类站的签到表单被 Turnstile 挡住：纯 HTTP 请求拿不到 token，POST 会被
服务端忽略。这个脚本在带 chromium 的容器里打开页面，等组件自动通过、页面 JS 自己
submit，再读回结果。

    docker run --rm --network host \
      -v /vol3/1000/SSD/Docker/telegrambot:/data \
      shiningth/sitebrowser:local ourbits

输出一行 JSON：{"state": "signed|already|failed", "detail": "...", "count": 12}
退出码：0 = 签上或今天已签；1 = 失败。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

CONFIG = Path("/data/config/config.json")

ALREADY_WORDS = ("已经签到", "请勿重复", "今天已签到", "今日已签到", "您今天已经")
SIGNED_RE = re.compile(r"第\s*(\d+)\s*次签到")
BONUS_RE = re.compile(r"本次签到获得\s*([\d,]+)\s*个魔力值")
LOGIN_WORDS = ("未登录", "必须在登录后", "login.php")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def load_site(site_id: str) -> dict:
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    for site in data.get("site_checkin", {}).get("sites", []):
        if site.get("id") == site_id:
            return site
    raise SystemExit(f"site {site_id!r} not found in {CONFIG}")


def parse_cookie(text: str) -> list[dict]:
    out = []
    for part in (text or "").replace("\n", ";").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        out.append({"name": name.strip(), "value": value.strip()})
    return out


def build_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    opts = Options()
    opts.binary_location = "/usr/bin/chromium"
    headless = os.environ.get("HEADLESS", "1") != "0"
    for arg in (
        *(("--headless=new",) if headless else ()),
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1280,900",
        "--lang=zh-CN",
        f"--user-agent={UA}",
    ):
        opts.add_argument(arg)
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    service = Service("/usr/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(60)
    return driver


def main() -> int:
    site_id = sys.argv[1] if len(sys.argv) > 1 else "ourbits"
    site = load_site(site_id)
    url = site["url"]
    host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
    cookies = parse_cookie(site.get("cookie", ""))

    result = {"state": "failed", "detail": "", "count": None, "final_url": url}
    driver = build_driver()
    try:
        driver.get(url)
        for c in cookies:
            try:
                driver.add_cookie({**c, "domain": host, "path": "/"})
            except Exception:  # noqa: BLE001
                pass
        if cookies:
            driver.get(url)

        blob = ""
        for _ in range(30):
            driver.implicitly_wait(0)
            try:
                blob = driver.find_element("tag name", "body").text
            except Exception:  # noqa: BLE001
                blob = ""
            if any(w in blob for w in ALREADY_WORDS) or SIGNED_RE.search(blob):
                break
            # Turnstile 有时需要一个真实点击
            try:
                frames = driver.find_elements("css selector", "iframe[src*='challenges.cloudflare.com']")
                for frame in frames:
                    driver.switch_to.frame(frame)
                    boxes = driver.find_elements("css selector", "input[type=checkbox], label")
                    if boxes:
                        boxes[0].click()
                    driver.switch_to.default_content()
            except Exception:  # noqa: BLE001
                driver.switch_to.default_content()
            driver.implicitly_wait(1)
            import time

            time.sleep(2)

        result["final_url"] = driver.current_url
        count = SIGNED_RE.search(blob)
        bonus = BONUS_RE.search(blob)
        if count:
            result["count"] = int(count.group(1))
        if any(w in blob for w in ALREADY_WORDS):
            result["state"] = "already"
            result["detail"] = "今日已签到"
        elif count or bonus:
            result["state"] = "signed"
            bits = []
            if count:
                bits.append(f"第 {count.group(1)} 次签到")
            if bonus:
                bits.append(f"获得 {bonus.group(1)} 魔力值")
            result["detail"] = "，".join(bits) or "签到成功"
        elif any(w in blob for w in LOGIN_WORDS):
            result["state"] = "login"
            result["detail"] = "Cookie 失效"
        else:
            snippet = re.sub(r"\s+", " ", blob)[:140]
            result["detail"] = f"没有读到签到结果：{snippet}"
    finally:
        driver.quit()

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["state"] in ("signed", "already") else 1


if __name__ == "__main__":
    raise SystemExit(main())
