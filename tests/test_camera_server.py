import os
import unittest
from unittest.mock import patch

from camera_server import CameraStream, Config


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            config = Config.from_environment()
        self.assertEqual(config.port, 8080)
        self.assertEqual((config.width, config.height), (1920, 1080))
        self.assertEqual(config.sensor_mode, "2304:1296:10:P")
        self.assertEqual(config.autofocus_mode, "continuous")

    def test_invalid_quality_is_rejected(self):
        with patch.dict(os.environ, {"CAMERA_QUALITY": "101"}, clear=True):
            with self.assertRaisesRegex(ValueError, "CAMERA_QUALITY"):
                Config.from_environment()


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(
            host="127.0.0.1",
            port=8080,
            width=1280,
            height=720,
            framerate=20,
            quality=75,
            sensor_mode="2304:1296:10:P",
            autofocus_mode="continuous",
            camera_command="fake-camera",
        )

    def test_latest_frame_replaces_previous_frame(self):
        stream = CameraStream(self.config)
        stream._publish(b"first")
        stream._publish(b"second")
        frame, sequence = stream.wait_for_frame(0, timeout=0)
        self.assertEqual(frame, b"second")
        self.assertEqual(sequence, 2)
        self.assertTrue(stream.status()["online"])

    def test_client_count_never_becomes_negative(self):
        stream = CameraStream(self.config)
        stream.remove_client()
        stream.add_client()
        self.assertEqual(stream.status()["clients"], 1)

    def test_camera_can_be_disabled_and_enabled(self):
        stream = CameraStream(self.config)
        stream._publish(b"frame")

        disabled = stream.set_enabled(False)

        self.assertFalse(disabled["enabled"])
        self.assertEqual(disabled["state"], "disabled")
        self.assertIsNone(stream.latest_frame())
        enabled = stream.set_enabled(True)
        self.assertTrue(enabled["enabled"])
        self.assertEqual(enabled["state"], "starting")


if __name__ == "__main__":
    unittest.main()
