import json
import os
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from unittest.mock import patch

from auth import AuthConfig, Authenticator, hash_password
from camera_server import CameraHTTPServer, CameraStream, Config
from detection import DetectionEngine
from recording import RecordingManager
from settings import DeviceSettings, SettingsStore
from tests.fake_camera import FRAME


PASSWORD = "a-secure-test-password"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class FakeSystemController:
    def __init__(self):
        self.reboot_requests = 0
        self.update_requests = 0

    def schedule_reboot(self):
        self.reboot_requests += 1
        return True

    def schedule_update(self):
        self.update_requests += 1


class FakeSoftwareUpdater:
    def __init__(self):
        self.invalidations = 0
        self.payload = {
            "supported": True,
            "available": True,
            "can_update": True,
            "state": "available",
            "message": "A software update is available.",
            "repository": "github.com/example/camera-fork",
            "branch": "main",
            "current_version": "111111111111",
            "latest_version": "222222222222",
        }

    def status(self, *, force=False):
        return dict(self.payload)

    def invalidate(self):
        self.invalidations += 1


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
        cls.settings_store = SettingsStore(
            os.path.join(cls.recording_directory.name, "device-settings.json"),
            DeviceSettings(True, False, True, detection.motion_sensitivity),
        )
        auth_config = AuthConfig(
            username="admin",
            password_hash=hash_password(PASSWORD, n=2**12),
            session_seconds=3600,
            secure_cookie=False,
        )
        cls.authenticator = Authenticator(auth_config)
        cls.system_controller = FakeSystemController()
        cls.software_updater = FakeSoftwareUpdater()
        cls.server = CameraHTTPServer(
            ("127.0.0.1", 0),
            cls.stream,
            cls.authenticator,
            system_controller=cls.system_controller,
            software_updater=cls.software_updater,
            settings_store=cls.settings_store,
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.recording_directory.cleanup()

    def setUp(self):
        self.cookie, self.csrf = self.login()

    def login(self, username="admin", password=PASSWORD):
        request = Request(
            f"{self.base_url}/api/login",
            data=json.dumps({"username": username, "password": password}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=2) as response:
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        request = Request(
            f"{self.base_url}/api/session", headers={"Cookie": cookie}
        )
        with urlopen(request, timeout=2) as response:
            csrf = json.load(response)["csrf_token"]
        return cookie, csrf

    def request(self, path, *, authenticated=True, headers=None):
        request_headers = dict(headers or {})
        if authenticated:
            request_headers["Cookie"] = self.cookie
        return Request(f"{self.base_url}{path}", headers=request_headers)

    def request_json(
        self, path, payload=None, method="GET", *, csrf=True, authenticated=True
    ):
        body = None if payload is None else json.dumps(payload).encode()
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if authenticated:
            headers["Cookie"] = self.cookie
        if csrf and method not in {"GET", "HEAD", "OPTIONS"}:
            headers["X-CSRF-Token"] = self.csrf
        request = Request(
            f"{self.base_url}{path}", data=body, method=method, headers=headers
        )
        with urlopen(request, timeout=2) as response:
            return response.status, json.load(response)

    def test_login_page_and_protected_root(self):
        with urlopen(f"{self.base_url}/login", timeout=2) as response:
            self.assertIn(b"Sign in", response.read())

        opener = build_opener(NoRedirect)
        with self.assertRaises(HTTPError) as raised:
            opener.open(f"{self.base_url}/", timeout=2)
        self.assertEqual(raised.exception.code, 303)
        self.assertEqual(raised.exception.headers["Location"], "/login")

    def test_every_sensitive_get_requires_authentication(self):
        for path in (
            "/api/status",
            "/api/system",
            "/api/update",
            "/api/recordings",
            "/stream.mjpg",
            "/snapshot.jpg",
            "/healthz",
            "/app.js",
        ):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as raised:
                    urlopen(f"{self.base_url}{path}", timeout=2)
                self.assertEqual(raised.exception.code, 401)

    def test_index_and_security_headers(self):
        with urlopen(self.request("/"), timeout=2) as response:
            body = response.read()
            self.assertEqual(response.status, 200)
            self.assertIn(b"Sentinel", body)
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_login_cookie_is_hardened(self):
        request = Request(
            f"{self.base_url}/api/login",
            data=json.dumps({"username": "admin", "password": PASSWORD}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=2) as response:
            cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)

    def test_invalid_login_is_rejected(self):
        request = Request(
            f"{self.base_url}/api/login",
            data=json.dumps({"username": "admin", "password": "incorrect"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 401)

    def test_login_requires_json_content_type(self):
        request = Request(
            f"{self.base_url}/api/login",
            data=json.dumps({"username": "admin", "password": PASSWORD}).encode(),
            method="POST",
            headers={"Content-Type": "text/plain"},
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 400)

    def test_state_changes_require_csrf(self):
        with self.assertRaises(HTTPError) as raised:
            self.request_json(
                "/api/camera", {"enabled": False}, "POST", csrf=False
            )
        self.assertEqual(raised.exception.code, 403)

    def test_status_payload(self):
        self.stream._publish(b"new-frame")
        with urlopen(self.request("/api/status"), timeout=2) as response:
            payload = json.load(response)
            self.assertTrue(payload["online"])
            self.assertEqual(payload["resolution"], "1280 × 720")

    def test_system_payload(self):
        with urlopen(self.request("/api/system"), timeout=2) as response:
            payload = json.load(response)
        self.assertIn("hostname", payload)
        self.assertIn("cpu_percent", payload)
        self.assertIn("memory", payload)
        self.assertIn("disk", payload)

    def test_snapshot_is_not_cached(self):
        self.stream._publish(b"jpeg-data")
        with urlopen(self.request("/snapshot.jpg"), timeout=2) as response:
            self.assertEqual(response.read(), b"jpeg-data")
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_missing_route_is_404_after_authentication(self):
        with self.assertRaises(HTTPError) as raised:
            urlopen(self.request("/missing"), timeout=2)
        self.assertEqual(raised.exception.code, 404)

    def test_camera_and_detection_controls(self):
        _, camera = self.request_json("/api/camera", {"enabled": False}, "POST")
        self.assertFalse(camera["enabled"])
        self.request_json("/api/camera", {"enabled": True}, "POST")

        _, detection = self.request_json(
            "/api/detection",
            {"motion_sensitivity": 75, "ai_categories": ["person"]},
            "POST",
        )
        self.assertEqual(detection["motion"]["sensitivity"], 75)
        self.assertEqual(detection["ai"]["categories"], ["person"])
        self.assertEqual(self.settings_store.current.motion_sensitivity, 75)
        self.assertEqual(self.settings_store.current.ai_categories, ("person",))

        with self.assertRaises(HTTPError) as raised:
            self.request_json(
                "/api/detection", {"ai_categories": []}, "POST"
            )
        self.assertEqual(raised.exception.code, 400)

    def test_recording_lifecycle(self):
        self.request_json("/api/camera", {"enabled": True}, "POST")
        self.stream._publish(FRAME)
        status, active = self.request_json("/api/recordings/start", {}, "POST")
        self.assertEqual(status, 201)
        self.stream._publish(FRAME)
        _, saved = self.request_json("/api/recordings/stop", {}, "POST")
        self.assertEqual(saved["id"], active["id"])
        _, payload = self.request_json("/api/recordings")
        self.assertTrue(
            any(item["id"] == active["id"] for item in payload["recordings"])
        )

    def test_reboot_requires_explicit_confirmation(self):
        with self.assertRaises(HTTPError) as raised:
            self.request_json("/api/system/reboot", {"confirm": "no"}, "POST")
        self.assertEqual(raised.exception.code, 400)
        before = self.system_controller.reboot_requests
        status, payload = self.request_json(
            "/api/system/reboot", {"confirm": "reboot"}, "POST"
        )
        self.assertEqual(status, 202)
        self.assertTrue(payload["rebooting"])
        self.assertEqual(self.system_controller.reboot_requests, before + 1)

    def test_update_uses_configured_repository_and_requires_confirmation(self):
        with urlopen(self.request("/api/update"), timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["repository"], "github.com/example/camera-fork")

        with self.assertRaises(HTTPError) as raised:
            self.request_json("/api/update", {"confirm": "no"}, "POST")
        self.assertEqual(raised.exception.code, 400)

        before = self.system_controller.update_requests
        status, payload = self.request_json(
            "/api/update", {"confirm": "update"}, "POST"
        )
        self.assertEqual(status, 202)
        self.assertTrue(payload["updating"])
        self.assertEqual(self.system_controller.update_requests, before + 1)
        self.assertGreater(self.software_updater.invalidations, 0)

    def test_logout_invalidates_session(self):
        self.request_json("/api/logout", {}, "POST")
        with self.assertRaises(HTTPError) as raised:
            urlopen(self.request("/api/status"), timeout=2)
        self.assertEqual(raised.exception.code, 401)
