import json
import os
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from camera_server import CameraHTTPServer, CameraStream, Config
from detection import DetectionEngine
from recording import RecordingManager
from tests.fake_camera import FRAME


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.recording_directory = tempfile.TemporaryDirectory()
        config = Config(
            host="127.0.0.1",
            port=0,
            width=1280,
            height=720,
            framerate=20,
            quality=75,
            sensor_mode="2304:1296:10:P",
            autofocus_mode="continuous",
            camera_command="fake-camera",
        )
        with patch.dict(os.environ, {"AI_ENABLED": "false"}, clear=True):
            detection = DetectionEngine()
        recordings = RecordingManager(cls.recording_directory.name, fps=20)
        cls.stream = CameraStream(config, detection, recordings)
        cls.server = CameraHTTPServer(("127.0.0.1", 0), cls.stream)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.recording_directory.cleanup()

    def request_json(self, path, payload=None, method="GET"):
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body else {},
        )
        with urlopen(request, timeout=2) as response:
            return response.status, json.load(response)

    def test_index_and_security_headers(self):
        with urlopen(f"{self.base_url}/", timeout=2) as response:
            body = response.read()
            self.assertEqual(response.status, 200)
            self.assertIn(b"Sentinel", body)
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_status_payload(self):
        self.stream._publish(b"new-frame")
        with urlopen(f"{self.base_url}/api/status", timeout=2) as response:
            payload = json.load(response)
            self.assertTrue(payload["online"])
            self.assertEqual(payload["resolution"], "1280 × 720")

    def test_snapshot_is_not_cached(self):
        self.stream._publish(b"jpeg-data")
        with urlopen(f"{self.base_url}/snapshot.jpg", timeout=2) as response:
            self.assertEqual(response.read(), b"jpeg-data")
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_missing_route_is_404(self):
        with self.assertRaises(HTTPError) as raised:
            urlopen(f"{self.base_url}/missing", timeout=2)
        self.assertEqual(raised.exception.code, 404)

    def test_camera_and_detection_controls(self):
        _, camera = self.request_json("/api/camera", {"enabled": False}, "POST")
        self.assertFalse(camera["enabled"])
        self.request_json("/api/camera", {"enabled": True}, "POST")

        _, detection = self.request_json(
            "/api/detection", {"motion_sensitivity": 75}, "POST"
        )
        self.assertEqual(detection["motion"]["sensitivity"], 75)

    def test_recording_lifecycle(self):
        self.request_json("/api/camera", {"enabled": True}, "POST")
        self.stream._publish(FRAME)
        status, active = self.request_json("/api/recordings/start", {}, "POST")
        self.assertEqual(status, 201)
        self.stream._publish(FRAME)
        _, saved = self.request_json("/api/recordings/stop", {}, "POST")
        self.assertEqual(saved["id"], active["id"])
        _, payload = self.request_json("/api/recordings")
        self.assertTrue(any(item["id"] == active["id"] for item in payload["recordings"]))
