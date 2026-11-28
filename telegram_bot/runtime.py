from __future__ import annotations

import os
import platform
import shutil
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RuntimeMetrics:
    started_at: float = field(default_factory=time.time)
    messages_seen: int = 0
    messages_handled: int = 0
    failures: int = 0
    _last_wall: float = field(default_factory=time.time)
    _last_cpu: float = field(default_factory=time.process_time)
    _last_cpu_percent: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            cpu_now = time.process_time()
            wall_delta = now - self._last_wall
            if wall_delta >= 0.2:
                cpu_delta = max(cpu_now - self._last_cpu, 0.0)
                self._last_cpu_percent = min((cpu_delta / wall_delta) * 100.0, 100.0)
                self._last_wall = now
                self._last_cpu = cpu_now
            cpu_percent = round(self._last_cpu_percent, 2)
        return {
            "pid": os.getpid(),
            "platform": platform.platform(),
            "uptime_seconds": int(now - self.started_at),
            "cpu_percent": cpu_percent,
            "memory_bytes": _memory_bytes(),
            "messages_seen": self.messages_seen,
            "messages_handled": self.messages_handled,
            "failures": self.failures,
        }


def ensure_runtime_dirs(project_root: Path, dirs: list[str]) -> None:
    for value in dirs:
        path = Path(value)
        if not path.is_absolute():
            path = project_root / path
        path.mkdir(parents=True, exist_ok=True)


def _is_hex_prefix(value: str, length: int = 7) -> bool:
    return len(value) >= length and all(ch in "0123456789abcdefABCDEF" for ch in value[:length])


def _read_git_short_commit(project_root: Path) -> str:
    """Resolve current 7-char lowercase commit SHA from .git without spawning git."""
    try:
        dot_git = project_root / ".git"
        if dot_git.is_file():
            pointer = dot_git.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return ""
            target = Path(pointer[7:].strip())
            git_dir = target if target.is_absolute() else (project_root / target).resolve()
        elif dot_git.is_dir():
            git_dir = dot_git
        else:
            return ""

        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        sha = ""
        if head.startswith("ref:"):
            ref_name = head[4:].strip()
            ref_file = git_dir / ref_name
            if ref_file.is_file():
                sha = ref_file.read_text(encoding="utf-8").strip()
            else:
                packed_refs = git_dir / "packed-refs"
                if packed_refs.is_file():
                    for raw_line in packed_refs.read_text(encoding="utf-8").splitlines():
                        line = raw_line.strip()
                        if not line or line.startswith(("#", "^")):
                            continue
                        parts = line.split()
                        if len(parts) >= 2 and parts[1] == ref_name:
                            sha = parts[0]
                            break
        else:
            sha = head

        return sha[:7].lower() if _is_hex_prefix(sha) else ""
    except (OSError, ValueError):
        return ""


def app_version_info(project_root: Path) -> dict[str, str]:
    """Return structured version metadata for status API and WebUI display."""
    from telegram_bot import __version__ as code_version

    git_commit = _read_git_short_commit(project_root)
    env_commit = os.getenv("TGBOT_APP_COMMIT", "").strip()
    build = os.getenv("TGBOT_APP_VERSION", "").strip()

    commit = git_commit or (env_commit[:7].lower() if _is_hex_prefix(env_commit) else "")
    if not git_commit and build and not build.startswith("ci-"):
        version = build.lstrip("vV")
    else:
        version = code_version.lstrip("vV") or build.lstrip("vV")

    if version and commit:
        display = f"v{version} · {commit}"
    elif version:
        display = f"v{version}"
    elif commit:
        display = commit
    else:
        display = "dev"

    return {
        "version": version,
        "commit": commit,
        "build": build,
        "display": display,
    }


def system_snapshot(project_root: Path, app_uptime_seconds: int) -> dict[str, Any]:
    disk = shutil.disk_usage(project_root)
    total_memory = _total_memory_bytes()
    process_memory = _memory_bytes()
    memory_percent = round((process_memory / total_memory) * 100, 1) if total_memory else 0.0
    disk_percent = round((disk.used / disk.total) * 100, 1) if disk.total else 0.0
    upload_bytes, download_bytes = _network_totals()

    return {
        "device": {
            "hostname": socket.gethostname(),
            "system_platform": platform.release() or platform.system().lower(),
            "uptime_seconds": app_uptime_seconds,
            "os": platform.system().lower(),
            "architecture": platform.machine(),
        },
        "hardware": {
            "processor": platform.processor() or _linux_cpu_name() or "unknown",
            "cores": os.cpu_count() or 0,
            "memory_total_bytes": total_memory,
            "memory_used_bytes": process_memory,
            "disk_total_bytes": disk.total,
            "disk_used_bytes": disk.used,
            "disk_percent": disk_percent,
        },
        "stats": {
            "cpu_percent": 0.0,
            "memory_percent": memory_percent,
            "upload_bytes": upload_bytes,
            "download_bytes": download_bytes,
        },
    }


def _memory_bytes() -> int:
    if platform.system() == "Windows":
        return _windows_memory_bytes()
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        value = int(usage.ru_maxrss)
        if platform.system() == "Darwin":
            return value
        return value * 1024
    except Exception:
        return 0


def _total_memory_bytes() -> int:
    if platform.system() == "Windows":
        return _windows_total_memory_bytes()
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fp:
            for line in fp:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        return 0
    return 0


def _linux_cpu_name() -> str:
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as fp:
            for line in fp:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
                if line.lower().startswith("hardware"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        return ""
    return ""


def _network_totals() -> tuple[int, int]:
    if platform.system() == "Linux":
        try:
            with open("/proc/net/dev", "r", encoding="utf-8") as fp:
                return _parse_linux_network_totals(fp.read())
        except Exception:
            return 0, 0
    return 0, 0


def _parse_linux_network_totals(content: str) -> tuple[int, int]:
    upload = 0
    download = 0
    fallback_upload = 0
    fallback_download = 0
    for raw_line in content.splitlines():
        if ":" not in raw_line:
            continue
        name, values = raw_line.split(":", 1)
        interface = name.strip()
        parts = values.split()
        if len(parts) < 16:
            continue
        rx_bytes = int(parts[0])
        tx_bytes = int(parts[8])
        fallback_download += rx_bytes
        fallback_upload += tx_bytes
        if interface == "lo":
            continue
        download += rx_bytes
        upload += tx_bytes
    return upload or fallback_upload, download or fallback_download


def _windows_total_memory_bytes() -> int:
    try:
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys)
    except Exception:
        return 0
    return 0


def _windows_memory_bytes() -> int:
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32.dll")
        psapi = ctypes.WinDLL("psapi.dll")
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(ProcessMemoryCounters)
        handle = kernel32.GetCurrentProcess()
        ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        if ok:
            return int(counters.WorkingSetSize)
    except Exception:
        return 0
    return 0
