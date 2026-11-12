"""Keep container configuration in /data/config without discarding older installs."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

CONFIG_PATH = Path("/data/config/config.json")
LEGACY_PATH = Path("/data/config.json")


def migrate_config(target: Path = CONFIG_PATH, legacy: Path = LEGACY_PATH) -> bool:
    if target.exists() or not legacy.is_file():
        return False
    data = legacy.read_bytes()
    if not isinstance(json.loads(data), dict):
        raise ValueError("Legacy container configuration must be a JSON object")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".config-migrate-", delete=False) as fh:
            temporary = Path(fh.name)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        # A concurrent startup must never replace an existing authoritative config.
        try:
            os.link(temporary, target)
        except FileExistsError:
            return False
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    command = sys.argv[1:]
    if not command:
        command = [sys.executable, "-m", "telegram_bot", "--config", str(CONFIG_PATH)]
    if "--config" in command:
        index = command.index("--config")
        if index + 1 < len(command) and command[index + 1] == str(CONFIG_PATH):
            if migrate_config():
                print("Configuration migrated to /data/config/config.json; original retained.", flush=True)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
