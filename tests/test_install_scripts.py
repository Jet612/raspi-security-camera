import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallScriptTests(unittest.TestCase):
    def test_shell_scripts_have_valid_syntax(self):
        subprocess.run(
            [
                "bash",
                "-n",
                str(ROOT / "install.sh"),
                str(ROOT / "install-service.sh"),
                str(ROOT / "update.sh"),
            ],
            check=True,
        )

    def test_service_installer_help_is_safe_off_device(self):
        result = subprocess.run(
            ["bash", str(ROOT / "install-service.sh"), "--help"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Usage: ./install-service.sh", result.stdout)
        self.assertIn("--skip-dependencies", result.stdout)

    def test_update_help_is_safe_off_device(self):
        result = subprocess.run(
            ["bash", str(ROOT / "update.sh"), "--help"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("configured Git remote", result.stdout)
        self.assertIn("--check", result.stdout)


if __name__ == "__main__":
    unittest.main()
