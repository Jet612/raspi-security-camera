import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from recording import RecordingManager
from tests.fake_camera import FRAME


class RecordingManagerTests(unittest.TestCase):
    def test_recording_is_listed_and_can_be_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = RecordingManager(directory, fps=20, ffmpeg_binary=None)
            active = manager.start()
            manager.write(FRAME)
            manager.write(FRAME)
            saved = manager.stop()

            self.assertEqual(saved["id"], active["id"])
            self.assertEqual(saved["frames"], 2)
            self.assertFalse(saved["processing"])
            self.assertIn("FFmpeg is unavailable", saved["conversion_error"])
            recordings = manager.list()
            self.assertEqual(len(recordings), 1)
            self.assertEqual(list(manager.frames(active["id"])), [FRAME, FRAME])
            self.assertEqual(recordings[0]["format"], "mjpeg")

    def test_completed_recording_is_converted_to_mp4(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("FFmpeg is not installed")
        with tempfile.TemporaryDirectory() as directory:
            manager = RecordingManager(directory, fps=10, ffmpeg_binary=ffmpeg)
            active = manager.start()
            for _ in range(10):
                manager.write(FRAME)
            manager.stop()

            deadline = time.monotonic() + 15
            recording = None
            while time.monotonic() < deadline:
                recording = manager.list()[0]
                if not recording["processing"]:
                    break
                time.sleep(0.05)

            self.assertIsNotNone(recording)
            self.assertFalse(recording["processing"])
            self.assertEqual(recording["format"], "mp4")
            self.assertEqual(manager.path(active["id"]).suffix, ".mp4")
            self.assertFalse((Path(directory) / f"{active['id']}.mjpeg").exists())
            probe = subprocess.run(
                [
                    shutil.which("ffprobe") or "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_name,pix_fmt",
                    "-of",
                    "default=noprint_wrappers=1",
                    str(manager.path(active["id"])),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("codec_name=h264", probe.stdout)
            self.assertIn("pix_fmt=yuv420p", probe.stdout)

    def test_recovery_removes_raw_copy_after_completed_mp4_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            recording_id = "20260101T000000"
            raw = Path(directory) / f"{recording_id}.mjpeg"
            mp4 = Path(directory) / f"{recording_id}.mp4"
            raw.write_bytes(FRAME)
            mp4.write_bytes(b"finished-mp4")

            RecordingManager(directory, ffmpeg_binary="/usr/bin/false")

            self.assertFalse(raw.exists())
            self.assertTrue(mp4.exists())

    def test_invalid_id_cannot_escape_recording_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = RecordingManager(directory, ffmpeg_binary=None)
            with self.assertRaisesRegex(ValueError, "invalid recording id"):
                manager.path("../../outside")
            self.assertFalse((Path(directory) / "outside").exists())


if __name__ == "__main__":
    unittest.main()
