import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from system_control import SystemController, SystemMonitor


class SystemMonitorTests(unittest.TestCase):
    def test_linux_metrics_are_parsed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "proc").mkdir()
            (root / "sys/class/thermal/thermal_zone0").mkdir(parents=True)
            (root / "proc/uptime").write_text("90061.0 0.0\n")
            (root / "proc/meminfo").write_text(
                "MemTotal:       1000 kB\nMemAvailable:    250 kB\n"
            )
            (root / "proc/stat").write_text("cpu  100 0 100 800 0 0 0 0\n")
            (root / "sys/class/thermal/thermal_zone0/temp").write_text("52500\n")

            monitor = SystemMonitor(root)
            first = monitor.snapshot()
            self.assertEqual(first["uptime"], "1d 1h 1m")
            self.assertEqual(first["memory"]["used_percent"], 75.0)
            self.assertEqual(first["temperature_c"], 52.5)
            self.assertIsNone(first["cpu_percent"])

            (root / "proc/stat").write_text("cpu  150 0 150 900 0 0 0 0\n")
            second = monitor.snapshot()
            self.assertEqual(second["cpu_percent"], 50.0)


class SystemControllerTests(unittest.TestCase):
    def test_reboot_is_rejected_when_logind_does_not_authorize_it(self):
        controller = SystemController()
        unavailable = CompletedProcess([], 0, stdout='s "no"\n', stderr="")
        with patch("system_control.shutil.which", return_value="/usr/bin/busctl"):
            with patch("system_control.subprocess.run", return_value=unavailable):
                with self.assertRaisesRegex(RuntimeError, "not authorized"):
                    controller.schedule_reboot()

    def test_update_starts_only_the_fixed_update_unit(self):
        controller = SystemController()
        completed = CompletedProcess([], 0, stdout='o "/job/1"\n', stderr="")
        with patch("system_control.shutil.which", return_value="/usr/bin/busctl"):
            with patch("system_control.subprocess.run", return_value=completed) as run:
                controller.schedule_update()
        command = run.call_args.args[0]
        self.assertIn("StartUnit", command)
        self.assertIn("raspi-security-camera-update.service", command)
        self.assertNotIn("raspi-security-camera.service", command)
