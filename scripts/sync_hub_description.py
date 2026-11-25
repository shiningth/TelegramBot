"""把 README.md 同步到 Docker Hub 的 full_description（相对链接转绝对链接）。"""

from __future__ import annotations

import base64
import json
import pathlib
import re
import urllib.request

REPO = pathlib.Path("/vol3/1000/SSD/github/TelegramBot/TelegramBot-main")
RAW_BASE = "https://github.com/shiningth/TelegramBot/blob/main/"
CFG = pathlib.Path.home() / ".docker" / "config.json"
RESOURCE = "/v2/repositories/shiningth/tgbot/"


def credentials() -> tuple[str, str]:
    data = json.loads(CFG.read_text())
    auth = data["auths"]["https://index.docker.io/v1/"]["auth"]
    user, token = base64.b64decode(auth).decode().split(":", 1)
    return user, token


def absolutize(text: str) -> str:
    def fix(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        if target.startswith(("http://", "https://", "#", "mailto:")):
            return match.group(0)
        if target.startswith("<") or target.startswith("!"):
            return match.group(0)
        return f"[{label}]({RAW_BASE}{target.lstrip('./')})"

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", fix, text)
    # 裸路径引用（如 (docs/deployment.md) 之外的行内相对路径）保持原样即可
    return text


def main() -> None:
    body = REPO.joinpath("README.md").read_text(encoding="utf-8").strip()
    description = absolutize(body)
    user, token = credentials()
    login = urllib.request.Request(
        "https://hub.docker.com/v2/users/login/",
        data=json.dumps({"username": user, "password": token}).encode(),
        headers={"Content-Type": "application/json"},
    )
    jwt = json.loads(urllib.request.urlopen(login, timeout=30).read())["token"]
    patch = urllib.request.Request(
        "https://hub.docker.com" + RESOURCE,
        data=json.dumps({"full_description": description}).encode(),
        headers={"Authorization": f"JWT {jwt}", "Content-Type": "application/json"},
        method="PATCH",
    )
    result = json.loads(urllib.request.urlopen(patch, timeout=60).read())
    print("patched, server length:", len(result.get("full_description") or ""))
    print("readme length:", len(description))


if __name__ == "__main__":
    main()
