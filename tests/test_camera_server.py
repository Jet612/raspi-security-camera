import os
import unittest
from unittest.mock import patch

from camera_server import CameraStream, Config, TLSConfig


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            config = Config.from_environment()
        self.assertEqual(config.port, 8080)
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual((config.width, config.height), (1920, 1080))
        self.assertEqual(config.quality, 85)
        self.assertEqual((config.live_width, config.live_height), (960, 540))
        self.assertEqual(config.live_framerate, 10)
        self.assertEqual(config.live_quality, 55)
        self.assertEqual(config.sensor_mode, "2304:1296:10:P")
        self.assertEqual(config.autofocus_mode, "continuous")

    def test_invalid_quality_is_rejected(self):
        with patch.dict(os.environ, {"CAMERA_QUALITY": "101"}, clear=True):
            with self.assertRaisesRegex(ValueError, "CAMERA_QUALITY"):
                Config.from_environment()

    def test_invalid_live_preview_configuration_is_rejected(self):
        with patch.dict(os.environ, {"CAMERA_LIVE_QUALITY": "10"}, clear=True):
            with self.assertRaisesRegex(ValueError, "CAMERA_LIVE_QUALITY"):
                Config.from_environment()

    def test_plain_http_is_limited_to_loopback(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(TLSConfig.from_environment("127.0.0.1").enabled)
            with self.assertRaisesRegex(ValueError, "unencrypted non-loopback"):
                TLSConfig.from_environment("0.0.0.0")

    def test_https_proxy_must_bind_to_loopback(self):
        with patch.dict(
            os.environ, {"CAMERA_TRUST_PROXY_HTTPS": "true"}, clear=True
        ):
            self.assertTrue(
                TLSConfig.from_environment("127.0.0.1").secure_transport
            )
            with self.assertRaisesRegex(ValueError, "requires CAMERA_HOST"):
                TLSConfig.from_environment("0.0.0.0")


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
        self.assertEqual(stream.latest_frame(), b"second")
        self.assertEqual(stream.status()["capture_quality"], 75)
        self.assertEqual(stream.status()["live_resolution"], "960 × 540")

    def test_client_count_never_becomes_negative(self):
        stream = CameraStream(self.config)
        stream.remove_client()
        stream._publish(b"high-quality-frame")
        stream.add_client()
        self.assertEqual(stream.status()["clients"], 1)
        self.assertEqual(stream.preview._source_frame, b"high-quality-frame")
        stream.remove_client()
        self.assertIsNone(stream.preview._source_frame)

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

    def test_camera_can_restore_disabled_state(self):
        stream = CameraStream(self.config, initial_enabled=False)
        self.assertFalse(stream.status()["enabled"])
        self.assertEqual(stream.status()["state"], "disabled")

    def test_request_stop_signals_capture_workers_immediately(self):
        stream = CameraStream(self.config)

        stream.request_stop()

        self.assertTrue(stream._stop.is_set())
        self.assertTrue(stream._wake.is_set())


if __name__ == "__main__":
    unittest.main()
