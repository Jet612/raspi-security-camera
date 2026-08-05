"""Read Linux device telemetry and request a reboot through systemd-logind."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


LOG = logging.getLogger("camera.system")


def _read_key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition(":")
            if separator:
                values[key] = value.strip()
    except OSError:
        pass
    return values


def _format_uptime(seconds: float) -> str:
    total_minutes = max(0, int(seconds // 60))
    days, remaining = divmod(total_minutes, 1440)
    hours, minutes = divmod(remaining, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


class SystemMonitor:
    def __init__(self, root: Path = Path("/")) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._previous_cpu: tuple[int, int] | None = None
        self._hostname = socket.gethostname()
        self._os = self._os_name()
        self._kernel = platform.release()
        self._architecture = platform.machine()

    def snapshot(self) -> dict[str, object]:
        uptime = self._uptime()
        memory = self._memory()
        disk = shutil.disk_usage(self.root)
        load = os.getloadavg()
        temperature = self._temperature()
        return {
            "hostname": self._hostname,
            "os": self._os,
            "kernel": self._kernel,
            "architecture": self._architecture,
            "uptime_seconds": round(uptime),
            "uptime": _format_uptime(uptime),
            "cpu_percent": self._cpu_percent(),
            "load_average": [round(value, 2) for value in load],
            "temperature_c": temperature,
            "memory": memory,
            "disk": {
                "total_bytes": disk.total,
                "used_bytes": disk.used,
                "available_bytes": disk.free,
                "used_percent": round((disk.used / disk.total) * 100, 1) if disk.total else 0.0,
            },
            "server_time": datetime.now(timezone.utc).isoformat(),
        }

    def _uptime(self) -> float:
        try:
            return float((self.root / "proc/uptime").read_text().split()[0])
        except (OSError, ValueError, IndexError):
            return 0.0

    def _memory(self) -> dict[str, object]:
        values = _read_key_values(self.root / "proc/meminfo")

        def bytes_for(key: str) -> int:
            try:
                return int(values.get(key, "0 kB").split()[0]) * 1024
            except (ValueError, IndexError):
                return 0

        total = bytes_for("MemTotal")
        available = bytes_for("MemAvailable")
        used = max(0, total - available)
        return {
            "total_bytes": total,
            "used_bytes": used,
            "available_bytes": available,
            "used_percent": round((used / total) * 100, 1) if total else 0.0,
        }

    def _cpu_percent(self) -> float | None:
        try:
            fields = (self.root / "proc/stat").read_text().splitlines()[0].split()[1:]
            ticks = [int(value) for value in fields]
            total = sum(ticks)
            idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
        except (OSError, ValueError, IndexError):
            return None
        with self._lock:
            previous = self._previous_cpu
            self._previous_cpu = (total, idle)
        if previous is None or total <= previous[0]:
            return None
        total_delta = total - previous[0]
        idle_delta = idle - previous[1]
        return round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 1)

    def _temperature(self) -> float | None:
        candidates = [
            self.root / "sys/class/thermal/thermal_zone0/temp",
            self.root / "sys/class/hwmon/hwmon0/temp1_input",
        ]
        for path in candidates:
            try:
                value = float(path.read_text().strip())
                return round(value / 1000 if value > 500 else value, 1)
            except (OSError, ValueError):
                continue
        return None

    @staticmethod
    def _os_name() -> str:
        try:
            release = platform.freedesktop_os_release()
            return release.get("PRETTY_NAME", release.get("NAME", "Linux"))
        except OSError:
            return "Linux"


class SystemController:
    """Requests narrowly authorized device actions over D-Bus."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reboot_pending = False

    def schedule_reboot(self, delay: float = 1.0) -> bool:
        busctl = shutil.which("busctl")
        if busctl is None:
            raise RuntimeError("busctl is unavailable; install systemd")
        try:
            availability = subprocess.run(
                [
                    busctl,
                    "call",
                    "org.freedesktop.login1",
                    "/org/freedesktop/login1",
                    "org.freedesktop.login1.Manager",
                    "CanReboot",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("could not query device reboot availability") from exc
        if availability.returncode or '"yes"' not in availability.stdout:
            raise RuntimeError("device reboot is not authorized or unavailable")
        with self._lock:
            if self._reboot_pending:
                return False
            self._reboot_pending = True
        threading.Thread(
            target=self._reboot, args=(delay,), name="device-reboot", daemon=True
        ).start()
        return True

    def schedule_update(self) -> None:
        busctl = shutil.which("busctl")
        if busctl is None:
            raise RuntimeError("busctl is unavailable; install systemd")
        try:
            result = subprocess.run(
                [
                    busctl,
                    "call",
                    "org.freedesktop.systemd1",
                    "/org/freedesktop/systemd1",
                    "org.freedesktop.systemd1.Manager",
                    "StartUnit",
                    "ss",
                    "raspi-security-camera-update.service",
                    "replace",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("could not start the software update") from exc
        if result.returncode:
            LOG.error("Software update request failed: %s", result.stderr.strip())
            raise RuntimeError("software update is not authorized or unavailable")

    def _reboot(self, delay: float) -> None:
        time.sleep(delay)
        command = [
            shutil.which("busctl") or "/usr/bin/busctl",
            "call",
            "org.freedesktop.login1",
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager",
            "Reboot",
            "b",
            "false",
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=10, check=False
            )
            if result.returncode:
                detail = result.stderr.strip() or f"exit status {result.returncode}"
                LOG.error("Device reboot request failed: %s", detail)
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.error("Device reboot request failed: %s", exc)
        finally:
            with self._lock:
                self._reboot_pending = False
