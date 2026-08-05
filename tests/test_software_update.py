import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from software_update import SoftwareUpdater, _display_repository


ROOT = Path(__file__).resolve().parents[1]


def git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(directory), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class SoftwareUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.remote = root / "camera-fork.git"
        self.seed = root / "seed"
        self.installation = root / "installation"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "main", str(self.seed)], check=True, capture_output=True)
        git(self.seed, "config", "user.name", "Test User")
        git(self.seed, "config", "user.email", "test@example.com")
        (self.seed / "version.txt").write_text("one\n", encoding="utf-8")
        git(self.seed, "add", "version.txt")
        git(self.seed, "commit", "-m", "initial")
        git(self.seed, "remote", "add", "origin", str(self.remote))
        git(self.seed, "push", "-u", "origin", "main")
        subprocess.run(
            ["git", "clone", "--branch", "main", str(self.remote), str(self.installation)],
            check=True,
            capture_output=True,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def push_update(self):
        (self.seed / "version.txt").write_text("two\n", encoding="utf-8")
        git(self.seed, "commit", "-am", "update")
        git(self.seed, "push", "origin", "main")

    def test_detects_update_from_installations_own_origin(self):
        updater = SoftwareUpdater(self.installation, cache_seconds=0)
        current = updater.status()
        self.assertTrue(current["supported"])
        self.assertFalse(current["available"])
        self.assertEqual(current["repository"], "configured Git remote")

        self.push_update()
        available = updater.status(force=True)
        self.assertTrue(available["available"])
        self.assertTrue(available["can_update"])
        self.assertEqual(available["branch"], "main")

    def test_local_changes_block_dashboard_update(self):
        self.push_update()
        (self.installation / "local-note.txt").write_text("keep me\n", encoding="utf-8")
        status = SoftwareUpdater(self.installation, cache_seconds=0).status()
        self.assertEqual(status["state"], "blocked")
        self.assertFalse(status["can_update"])

    def test_local_commits_are_not_misreported_as_safe_updates(self):
        git(self.installation, "config", "user.name", "Test User")
        git(self.installation, "config", "user.email", "test@example.com")
        git(self.installation, "commit", "--allow-empty", "-m", "local change")
        status = SoftwareUpdater(self.installation, cache_seconds=0).status()
        self.assertTrue(status["available"])
        self.assertEqual(status["state"], "blocked")
        self.assertFalse(status["can_update"])

    def test_update_script_fast_forwards_without_overwriting(self):
        shutil.copy2(ROOT / "update.sh", self.installation / "update.sh")
        git(self.installation, "config", "user.name", "Test User")
        git(self.installation, "config", "user.email", "test@example.com")
        git(self.installation, "add", "update.sh")
        git(self.installation, "commit", "-m", "add updater")
        git(self.installation, "push", "origin", "main")
        git(self.seed, "pull", "--ff-only", "origin", "main")
        self.push_update()

        result = subprocess.run(
            ["bash", str(self.installation / "update.sh"), "--no-restart"],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
        )
        self.assertIn("Updated successfully", result.stdout)
        self.assertEqual(
            git(self.installation, "rev-parse", "HEAD"),
            git(self.seed, "rev-parse", "HEAD"),
        )

    def test_remote_labels_never_expose_credentials(self):
        self.assertEqual(
            _display_repository("https://secret-token@github.com/person/fork.git"),
            "github.com/person/fork",
        )
        self.assertEqual(
            _display_repository("git@github.com:person/fork.git"),
            "github.com/person/fork",
        )


if __name__ == "__main__":
    unittest.main()
