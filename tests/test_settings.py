import json
import os
import tempfile
import unittest
from pathlib import Path

from settings import DeviceSettings, SettingsStore


DEFAULTS = DeviceSettings(
    camera_enabled=True,
    ai_enabled=True,
    motion_enabled=True,
    motion_sensitivity=80,
)


class SettingsStoreTests(unittest.TestCase):
    def test_updates_survive_a_new_store_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device-settings.json"
            store = SettingsStore(path, DEFAULTS)

            store.update(
                camera_enabled=False,
                ai_enabled=False,
                motion_enabled=True,
                motion_sensitivity=67,
                ai_categories=["person"],
            )

            restored = SettingsStore(path, DEFAULTS).current
            self.assertFalse(restored.camera_enabled)
            self.assertFalse(restored.ai_enabled)
            self.assertTrue(restored.motion_enabled)
            self.assertEqual(restored.motion_sensitivity, 67)
            self.assertEqual(restored.ai_categories, ("person",))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_rejects_invalid_or_overexposed_settings_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device-settings.json"
            path.write_text(json.dumps({"version": 1}))
            path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "missing or unknown"):
                SettingsStore(path, DEFAULTS)

            path.write_text(json.dumps(DEFAULTS.to_json()))
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "group or others"):
                SettingsStore(path, DEFAULTS)

    def test_rejects_non_integer_sensitivity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json", DEFAULTS)
            with self.assertRaisesRegex(ValueError, "integer"):
                store.update(motion_sensitivity=50.5)  # type: ignore[arg-type]

    def test_rejects_empty_or_unknown_ai_categories(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json", DEFAULTS)
            with self.assertRaisesRegex(ValueError, "at least one"):
                store.update(ai_categories=[])
            with self.assertRaisesRegex(ValueError, "person, vehicle, or animal"):
                store.update(ai_categories=["package"])

    def test_version_one_settings_migrate_to_all_ai_categories(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "camera_enabled": True,
                        "ai_enabled": True,
                        "motion_enabled": False,
                        "motion_sensitivity": 80,
                    }
                )
            )
            path.chmod(0o600)
            restored = SettingsStore(path, DEFAULTS).current
            self.assertEqual(restored.ai_categories, ("person", "vehicle", "animal"))


if __name__ == "__main__":
    unittest.main()
