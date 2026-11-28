from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from telegram_bot import __version__
from telegram_bot.app import TelegramBotApplication
from telegram_bot.config import AppConfig
from telegram_bot.runtime import _parse_linux_network_totals, app_version_info


def test_status_snapshot_keeps_dashboard_history() -> None:
    with TemporaryDirectory() as tmp:
        app = TelegramBotApplication(AppConfig(), Path(tmp) / "config.json")

        first = app.status_snapshot()
        second = app.status_snapshot()

        assert len(second["history"]) >= len(first["history"]) >= 1
        assert {"ts", "cpu", "memory", "upload_bytes", "download_bytes"} <= set(second["history"][-1])


def test_linux_network_totals_skip_loopback() -> None:
    content = """
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 1000       1    0    0    0     0          0         0 2000       1    0    0    0     0       0          0
  eth0: 3000       2    0    0    0     0          0         0 5000       2    0    0    0     0       0          0
"""

    upload, download = _parse_linux_network_totals(content)

    assert upload == 5000
    assert download == 3000


def test_app_version_info_prefers_git_head_branch_ref(tmp_path: Path, monkeypatch) -> None:
    git_dir = tmp_path / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text("d25c5fb1234567890abcdef1234567890abcdef1\n", encoding="utf-8")
    monkeypatch.setenv("TGBOT_APP_VERSION", "2026.12.01-1")
    monkeypatch.setenv("TGBOT_APP_COMMIT", "00000001111111")

    info = app_version_info(tmp_path)

    assert info["version"] == __version__
    assert info["commit"] == "d25c5fb"
    assert info["build"] == "2026.12.01-1"
    assert info["display"] == f"v{__version__} · d25c5fb"


def test_app_version_info_supports_detached_head_and_packed_refs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TGBOT_APP_VERSION", raising=False)
    monkeypatch.delenv("TGBOT_APP_COMMIT", raising=False)

    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ABCDEF1234567890abcdef1234567890abcdef12\n", encoding="utf-8")
    assert app_version_info(tmp_path)["commit"] == "abcdef1"

    (git_dir / "HEAD").write_text("ref: refs/heads/release\n", encoding="utf-8")
    (git_dir / "packed-refs").write_text(
        "# pack-refs with: peeled\n1234567890abcdef1234567890abcdef12345678 refs/heads/release\n",
        encoding="utf-8",
    )
    assert app_version_info(tmp_path)["commit"] == "1234567"


def test_app_version_info_falls_back_to_build_env_without_git(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TGBOT_APP_VERSION", "ci-20261128-051800-d25c5fb")
    monkeypatch.setenv("TGBOT_APP_COMMIT", "D25C5FB1234567890")

    ci_info = app_version_info(tmp_path)
    assert ci_info["version"] == __version__
    assert ci_info["commit"] == "d25c5fb"
    assert ci_info["display"] == f"v{__version__} · d25c5fb"

    monkeypatch.setenv("TGBOT_APP_VERSION", "2026.12.01-1")
    release_info = app_version_info(tmp_path)
    assert release_info["version"] == "2026.12.01-1"
    assert release_info["commit"] == "d25c5fb"
    assert release_info["display"] == "v2026.12.01-1 · d25c5fb"


def test_status_snapshot_exposes_project_version_fields(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("deadbeef00112233445566778899aabbccddeeff\n", encoding="utf-8")

    app = TelegramBotApplication(AppConfig(), tmp_path / "config.json")
    app.project_root = tmp_path
    project = app.status_snapshot()["project"]

    assert project["version"] == f"v{__version__} · deadbee"
    assert project["app_version"] == __version__
    assert project["commit"] == "deadbee"
    assert "build" in project
