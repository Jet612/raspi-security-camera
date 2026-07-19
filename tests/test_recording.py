import tempfile
import unittest
from pathlib import Path

from recording import RecordingManager
from tests.fake_camera import FRAME


class RecordingManagerTests(unittest.TestCase):
    def test_recording_is_listed_and_can_be_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = RecordingManager(directory, fps=20)
            active = manager.start()
            manager.write(FRAME)
            manager.write(FRAME)
            saved = manager.stop()

            self.assertEqual(saved["id"], active["id"])
            self.assertEqual(saved["frames"], 2)
            recordings = manager.list()
            self.assertEqual(len(recordings), 1)
            self.assertEqual(list(manager.frames(active["id"])), [FRAME, FRAME])

    def test_invalid_id_cannot_escape_recording_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = RecordingManager(directory)
            with self.assertRaisesRegex(ValueError, "invalid recording id"):
                manager.path("../../outside")
            self.assertFalse((Path(directory) / "outside").exists())


if __name__ == "__main__":
    unittest.main()
