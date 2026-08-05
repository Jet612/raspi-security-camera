#!/usr/bin/env python3
"""Dependency-free MJPEG web server for a Raspberry Pi camera."""

from __future__ import annotations

import argparse
import getpass
import gzip
import ipaddress
import json
import logging
import os
import shlex
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from auth import AuthConfig, AuthSession, Authenticator, LoginRateLimited, hash_password
from detection import DetectionEngine
from preview import LivePreview
from recording import RecordingManager
from settings import DeviceSettings, SettingsStore
from software_update import SoftwareUpdater
from system_control import SystemController, SystemMonitor


LOG = logging.getLogger("camera")
ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"
JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"
MAX_JPEG_BYTES = 32 * 1024 * 1024


@lru_cache(maxsize=16)
def _read_static_file(filename: str) -> bytes:
    """Keep versioned frontend assets in memory after their first request."""
    return (STATIC_ROOT / filename).read_bytes()


@lru_cache(maxsize=16)
def _gzip_static_file(filename: str) -> bytes:
    return gzip.compress(_read_static_file(filename), compresslevel=6)


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    width: int
    height: int
    framerate: int
    quality: int
    sensor_mode: str
    autofocus_mode: str
    camera_command: str | None
    live_width: int = 960
    live_height: int = 540
    live_framerate: int = 10
    live_quality: int = 55

    @classmethod
    def from_environment(cls) -> "Config":
        autofocus_mode = os.getenv("CAMERA_AF_MODE", "continuous")
        if autofocus_mode not in {"default", "manual", "auto", "continuous"}:
            raise ValueError(
                "CAMERA_AF_MODE must be default, manual, auto, or continuous"
            )
        return cls(
            host=os.getenv("CAMERA_HOST", "127.0.0.1"),
            port=env_int("CAMERA_PORT", 8080, 1, 65535),
            width=env_int("CAMERA_WIDTH", 1920, 320, 4608),
            height=env_int("CAMERA_HEIGHT", 1080, 240, 2592),
            framerate=env_int("CAMERA_FPS", 20, 1, 60),
            quality=env_int("CAMERA_QUALITY", 85, 1, 100),
            sensor_mode=os.getenv("CAMERA_SENSOR_MODE", "2304:1296:10:P"),
            autofocus_mode=autofocus_mode,
            camera_command=os.getenv("CAMERA_COMMAND") or None,
            live_width=env_int("CAMERA_LIVE_WIDTH", 960, 320, 1920),
            live_height=env_int("CAMERA_LIVE_HEIGHT", 540, 240, 1080),
            live_framerate=env_int("CAMERA_LIVE_FPS", 10, 1, 30),
            live_quality=env_int("CAMERA_LIVE_QUALITY", 55, 20, 90),
        )

    def capture_argv(self) -> list[str]:
        if self.camera_command:
            return shlex.split(self.camera_command)

        binary = shutil.which("rpicam-vid") or shutil.which("libcamera-vid")
        if not binary:
            raise FileNotFoundError(
                "rpicam-vid was not found; install the Raspberry Pi rpicam-apps package"
            )

        return [
            binary,
            "--timeout",
            "0",
            "--nopreview",
            "--codec",
            "mjpeg",
            "--width",
            str(self.width),
            "--height",
            str(self.height),
            "--framerate",
            str(self.framerate),
            "--quality",
            str(self.quality),
            "--mode",
            self.sensor_mode,
            "--autofocus-mode",
            self.autofocus_mode,
            "--output",
            "-",
        ]


@dataclass(frozen=True)
class TLSConfig:
    certificate: str | None
    private_key: str | None
    trusted_https_proxy: bool

    @property
    def enabled(self) -> bool:
        return self.certificate is not None

    @property
    def secure_transport(self) -> bool:
        return self.enabled or self.trusted_https_proxy

    @classmethod
    def from_environment(cls, host: str) -> "TLSConfig":
        certificate = os.getenv("CAMERA_TLS_CERT") or None
        private_key = os.getenv("CAMERA_TLS_KEY") or None
        if bool(certificate) != bool(private_key):
            raise ValueError("CAMERA_TLS_CERT and CAMERA_TLS_KEY must be set together")
        trusted_proxy = env_bool("CAMERA_TRUST_PROXY_HTTPS", False)
        if trusted_proxy and not is_loopback_host(host):
            raise ValueError(
                "CAMERA_TRUST_PROXY_HTTPS requires CAMERA_HOST to be a loopback address"
            )
        if not certificate and not trusted_proxy and not is_loopback_host(host):
            raise ValueError(
                "refusing unencrypted non-loopback access; configure TLS or bind "
                "CAMERA_HOST to 127.0.0.1 behind an HTTPS proxy"
            )
        return cls(certificate, private_key, trusted_proxy)

    def context(self) -> ssl.SSLContext | None:
        if not self.enabled:
            return None
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.options |= ssl.OP_NO_COMPRESSION
        try:
            context.load_cert_chain(self.certificate, self.private_key)
        except (OSError, ssl.SSLError) as exc:
            raise ValueError("could not load CAMERA_TLS_CERT/CAMERA_TLS_KEY") from exc
        return context


class CameraStream:
    """Owns one capture process and publishes only its most recent JPEG."""

    def __init__(
        self,
        config: Config,
        detection: DetectionEngine | None = None,
        recordings: RecordingManager | None = None,
        *,
        initial_enabled: bool = True,
    ) -> None:
        self.config = config
        self.detection = detection
        self.recordings = recordings or RecordingManager(fps=config.framerate)
        self.preview = LivePreview(
            width=config.live_width,
            height=config.live_height,
            fps=config.live_framerate,
            quality=config.live_quality,
        )
        self._condition = threading.Condition()
        self._frame: bytes | None = None
        self._sequence = 0
        self._frame_at = 0.0
        self._frame_times: deque[float] = deque(maxlen=max(120, config.framerate * 6))
        self._clients = 0
        self._state = "starting" if initial_enabled else "disabled"
        self._error: str | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._enabled = initial_enabled
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(
            target=self._supervise, name="camera-capture", daemon=True
        )

    def start(self) -> None:
        self.preview.start()
        self._thread.start()

    def stop(self) -> None:
        self.request_stop()
        process = self._process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        self._thread.join(timeout=5)
        self.preview.stop()
        self.recordings.close()

    def request_stop(self) -> None:
        """Signal workers immediately without waiting for their cleanup."""
        self._stop.set()
        self._wake.set()
        with self._condition:
            self._condition.notify_all()

    def set_enabled(self, enabled: bool) -> dict[str, object]:
        """Turn physical capture on or off while keeping the dashboard available."""
        with self._condition:
            if enabled == self._enabled:
                return self.status()
            self._enabled = enabled
            self._frame = None
            self._frame_at = 0.0
            self._frame_times.clear()
            self._sequence += 1
            self._state = "starting" if enabled else "disabled"
            self._error = None
            self._condition.notify_all()
        self.preview.clear()
        self._wake.set()
        if not enabled:
            self.recordings.stop()
            process = self._process
            if process and process.poll() is None:
                process.terminate()
        return self.status()

    def _set_state(self, state: str, error: str | None = None) -> None:
        with self._condition:
            self._state = state
            self._error = error
            self._condition.notify_all()

    def _supervise(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            if not self._enabled:
                self._set_state("disabled")
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            try:
                argv = self.config.capture_argv()
                LOG.info("Starting camera capture: %s", shlex.join(argv))
                self._set_state("starting")
                self._process = subprocess.Popen(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=None,
                    bufsize=0,
                )
                self._read_frames(self._process)
                return_code = self._process.wait()
                if self._stop.is_set():
                    break
                if not self._enabled:
                    continue
                raise RuntimeError(f"camera process exited with status {return_code}")
            except (FileNotFoundError, OSError, RuntimeError) as exc:
                message = str(exc)
                LOG.error("Camera unavailable: %s", message)
                self._set_state("offline", message)
                if self._wake.wait(delay):
                    self._wake.clear()
                if self._stop.is_set():
                    break
                delay = min(delay * 2, 15.0)
            finally:
                self._process = None
        self._set_state("stopped")

    def _read_frames(self, process: subprocess.Popen[bytes]) -> None:
        if process.stdout is None:
            raise RuntimeError("camera process did not provide a video stream")

        buffer = bytearray()
        while not self._stop.is_set():
            chunk = process.stdout.read(64 * 1024)
            if not chunk:
                break
            buffer.extend(chunk)

            while True:
                start = buffer.find(JPEG_START)
                if start < 0:
                    if len(buffer) > 1:
                        del buffer[:-1]
                    break
                if start:
                    del buffer[:start]
                end = buffer.find(JPEG_END, 2)
                if end < 0:
                    if len(buffer) > MAX_JPEG_BYTES:
                        LOG.warning("Discarding an oversized or incomplete JPEG frame")
                        buffer.clear()
                    break
                end += len(JPEG_END)
                frame = bytes(buffer[:end])
                del buffer[:end]
                self._publish(frame)

    def _publish(self, frame: bytes) -> None:
        now = time.monotonic()
        with self._condition:
            self._frame = frame
            self._frame_at = now
            self._frame_times.append(now)
            self._sequence += 1
            self._state = "online"
            self._error = None
            has_viewers = self._clients > 0
            self._condition.notify_all()
        if self.detection is not None:
            self.detection.submit(frame)
        self.recordings.write(frame)
        if has_viewers:
            self.preview.submit(frame)

    def wait_for_frame(
        self, last_sequence: int, timeout: float = 10.0
    ) -> tuple[bytes | None, int]:
        with self._condition:
            self._condition.wait_for(
                lambda: self._sequence != last_sequence or self._stop.is_set(),
                timeout=timeout,
            )
            return self._frame, self._sequence

    def latest_frame(self) -> bytes | None:
        with self._condition:
            return self._frame

    def wait_for_preview(
        self, last_sequence: int, timeout: float = 10.0
    ) -> tuple[bytes | None, int]:
        return self.preview.wait_for_frame(last_sequence, timeout)

    def add_client(self) -> None:
        with self._condition:
            self._clients += 1
            frame = self._frame
        if frame is not None:
            self.preview.submit(frame)

    def remove_client(self) -> None:
        with self._condition:
            self._clients = max(0, self._clients - 1)
            has_viewers = self._clients > 0
        if not has_viewers:
            self.preview.clear()

    def status(self) -> dict[str, object]:
        now = time.monotonic()
        with self._condition:
            recent = [stamp for stamp in self._frame_times if now - stamp <= 5.0]
            fps = 0.0
            if len(recent) > 1:
                fps = (len(recent) - 1) / (recent[-1] - recent[0])
            age = None if not self._frame_at else max(0.0, now - self._frame_at)
            online = self._state == "online" and age is not None and age < 3.0
            status = {
                "enabled": self._enabled,
                "online": online,
                "state": self._state,
                "fps": round(fps, 1),
                "resolution": f"{self.config.width} × {self.config.height}",
                "clients": self._clients,
                "last_frame_age_ms": None if age is None else round(age * 1000),
                "error": self._error,
                "server_time": datetime.now(timezone.utc).isoformat(),
            }
        if self.detection is not None:
            status["detection"] = self.detection.status()
        status["recording"] = self.recordings.status()
        preview = self.preview.status()
        status["live_fps"] = preview["fps"]
        status["live_resolution"] = preview["resolution"]
        status["live_quality"] = preview["quality"]
        status["capture_resolution"] = status["resolution"]
        status["capture_quality"] = self.config.quality
        return status


class CameraHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        stream: CameraStream,
        authenticator: Authenticator,
        *,
        system_monitor: SystemMonitor | None = None,
        system_controller: SystemController | None = None,
        software_updater: SoftwareUpdater | None = None,
        settings_store: SettingsStore | None = None,
        secure_transport: bool = False,
    ) -> None:
        self.stream = stream
        self.authenticator = authenticator
        self.system_monitor = system_monitor or SystemMonitor()
        self.system_controller = system_controller or SystemController()
        self.software_updater = software_updater or SoftwareUpdater(ROOT)
        self.settings_store = settings_store
        self._settings_lock = threading.Lock()
        self.secure_transport = secure_transport
        super().__init__(address, CameraRequestHandler)

    def set_camera_enabled(self, enabled: bool) -> dict[str, object]:
        with self._settings_lock:
            if self.settings_store is not None:
                self.settings_store.update(camera_enabled=enabled)
            return self.stream.set_enabled(enabled)

    def set_detection(
        self,
        *,
        ai: bool | None,
        motion: bool | None,
        sensitivity: float | None,
        categories: list[str] | None,
    ) -> dict[str, object]:
        detection = self.stream.detection
        if detection is None:
            raise RuntimeError("detection is unavailable")
        sensitivity_value = round(sensitivity) if sensitivity is not None else None
        if sensitivity is not None and sensitivity != sensitivity_value:
            raise ValueError("motion_sensitivity must be an integer")
        with self._settings_lock:
            if self.settings_store is not None:
                self.settings_store.update(
                    ai_enabled=ai,
                    motion_enabled=motion,
                    motion_sensitivity=sensitivity_value,
                    ai_categories=categories,
                )
            return detection.set_enabled(
                ai=ai,
                motion=motion,
                sensitivity=sensitivity_value,
                categories=categories,
            )

    def get_request(self) -> tuple[object, object]:
        request, address = super().get_request()
        request.settimeout(15)
        return request, address


class CameraRequestHandler(BaseHTTPRequestHandler):
    server: CameraHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "Sentinel"
    sys_version = ""
    static_files = {
        "/camera": ("camera.html", "text/html; charset=utf-8"),
        "/camera.html": ("camera.html", "text/html; charset=utf-8"),
        "/recordings": ("recordings.html", "text/html; charset=utf-8"),
        "/recordings.html": ("recordings.html", "text/html; charset=utf-8"),
        "/settings": ("settings.html", "text/html; charset=utf-8"),
        "/settings.html": ("settings.html", "text/html; charset=utf-8"),
        "/system": ("system.html", "text/html; charset=utf-8"),
        "/system.html": ("system.html", "text/html; charset=utf-8"),
        "/login": ("login.html", "text/html; charset=utf-8"),
        "/styles.css": ("styles.css", "text/css; charset=utf-8"),
        "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/login.js": ("login.js", "text/javascript; charset=utf-8"),
        "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    }
    public_files = {"/styles.css", "/login.js", "/favicon.svg"}
    private_cache_files = {"/app.js"}

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            # Browsers routinely close idle keep-alive and MJPEG connections.
            # Treat that as normal client behavior instead of a server error.
            pass

    def log_message(self, fmt: str, *args: object) -> None:
        LOG.info("%s - %s", self.client_address[0], fmt % args)

    def log_error(self, fmt: str, *args: object) -> None:
        if fmt.startswith("Request timed out"):
            return
        super().log_error(fmt, *args)

    def do_GET(self) -> None:  # noqa: N802
        request_url = urlsplit(self.path)
        path = request_url.path
        existing_session = self._current_session()
        if path in {"/", "/index.html"}:
            if existing_session is None:
                self._redirect("/login")
            else:
                self._redirect("/camera")
            return
        if path == "/login":
            if existing_session is not None:
                self._redirect("/camera")
            else:
                self._serve_static(*self.static_files[path], cache_public=False)
            return
        if path in self.public_files:
            self._serve_static(*self.static_files[path], cache_public=True)
            return
        session = existing_session or self._require_auth(path)
        if session is None:
            return
        if path == "/stream.mjpg":
            self._serve_stream(session)
        elif path == "/api/status":
            self._send_json(self.server.stream.status())
        elif path == "/api/session":
            self._send_json(
                {"username": session.username, "csrf_token": session.csrf_token}
            )
        elif path == "/api/system":
            self._send_json(self.server.system_monitor.snapshot())
        elif path == "/api/update":
            force = parse_qs(request_url.query).get("refresh") == ["1"]
            self._send_json(self.server.software_updater.status(force=force))
        elif path == "/api/recordings":
            self._send_json({"recordings": self.server.stream.recordings.list()})
        elif path == "/healthz":
            status = self.server.stream.status()
            code = HTTPStatus.OK if status["online"] else HTTPStatus.SERVICE_UNAVAILABLE
            self._send_json(status, code)
        elif path == "/snapshot.jpg":
            self._serve_snapshot()
        elif path.startswith("/api/recordings/") and path.endswith("/stream.mjpg"):
            recording_id = path.split("/")[3]
            self._serve_recording(recording_id, session)
        elif path.startswith("/api/recordings/") and path.endswith("/download"):
            recording_id = path.split("/")[3]
            self._serve_recording_download(recording_id, session)
        elif path in self.static_files:
            self._serve_static(
                *self.static_files[path],
                cache_public=False,
                cache_private=path in self.private_cache_files,
            )
        else:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/login":
            self._login()
            return
        session = self._require_auth(path)
        if session is None or not self._require_csrf(session):
            return
        try:
            if path == "/api/logout":
                self.server.authenticator.logout(session)
                self._send_json(
                    {"logged_out": True},
                    headers={
                        "Set-Cookie": self.server.authenticator.clear_cookie_header(),
                        "Clear-Site-Data": '"cache", "cookies", "storage"',
                    },
                )
            elif path == "/api/camera":
                payload = self._read_json()
                enabled = self._required_bool(payload, "enabled")
                self._send_json(self.server.set_camera_enabled(enabled))
            elif path == "/api/detection":
                payload = self._read_json()
                ai = self._optional_bool(payload, "ai_enabled")
                motion = self._optional_bool(payload, "motion_enabled")
                sensitivity = self._optional_number(payload, "motion_sensitivity")
                categories = self._optional_string_list(payload, "ai_categories")
                if (
                    ai is None
                    and motion is None
                    and sensitivity is None
                    and categories is None
                ):
                    raise ValueError(
                        "ai_enabled, ai_categories, motion_enabled, or "
                        "motion_sensitivity is required"
                    )
                self._send_json(
                    self.server.set_detection(
                        ai=ai,
                        motion=motion,
                        sensitivity=sensitivity,
                        categories=categories,
                    )
                )
            elif path == "/api/recordings/start":
                if not self.server.stream.status()["online"]:
                    self._send_json(
                        {"error": "the camera must be online to start recording"},
                        HTTPStatus.CONFLICT,
                    )
                    return
                recording = self.server.stream.recordings.start()
                self._send_json(recording, HTTPStatus.CREATED)
            elif path == "/api/recordings/stop":
                recording = self.server.stream.recordings.stop()
                if recording is None:
                    self._send_json(
                        {"error": "no recording is active"}, HTTPStatus.CONFLICT
                    )
                    return
                self._send_json(recording)
            elif path == "/api/system/reboot":
                payload = self._read_json()
                if payload.get("confirm") != "reboot":
                    raise ValueError("reboot confirmation is required")
                scheduled = self.server.system_controller.schedule_reboot()
                self._send_json(
                    {"rebooting": True, "already_pending": not scheduled},
                    HTTPStatus.ACCEPTED,
                )
            elif path == "/api/update":
                payload = self._read_json()
                if payload.get("confirm") != "update":
                    raise ValueError("update confirmation is required")
                update_status = self.server.software_updater.status(force=True)
                if not update_status.get("available"):
                    self._send_json(
                        {"error": "the app is already up to date"},
                        HTTPStatus.CONFLICT,
                    )
                    return
                if not update_status.get("can_update"):
                    self._send_json(
                        {
                            "error": str(
                                update_status.get("message", "the update is blocked")
                            )
                        },
                        HTTPStatus.CONFLICT,
                    )
                    return
                self.server.system_controller.schedule_update()
                self.server.software_updater.invalidate()
                self._send_json({"updating": True}, HTTPStatus.ACCEPTED)
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except OSError:
            LOG.exception("Device control failed")
            self._send_json(
                {"error": "device control failed"}, HTTPStatus.INTERNAL_SERVER_ERROR
            )
        except RuntimeError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        session = self._require_auth(path)
        if session is None or not self._require_csrf(session):
            return
        if not path.startswith("/api/recordings/"):
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        recording_id = path.split("/")[3]
        try:
            removed = self.server.stream.recordings.delete(recording_id)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except RuntimeError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
            return
        if not removed:
            self._send_json({"error": "recording not found"}, HTTPStatus.NOT_FOUND)
            return
        self._send_json({"deleted": recording_id})

    def _login(self) -> None:
        try:
            payload = self._read_json()
            username, password = payload.get("username"), payload.get("password")
            if not isinstance(username, str) or not isinstance(password, str):
                raise ValueError("username and password are required")
            session = self.server.authenticator.login(
                username, password, self.client_address[0]
            )
        except LoginRateLimited as exc:
            self._send_json(
                {"error": "too many login attempts; try again shortly"},
                HTTPStatus.TOO_MANY_REQUESTS,
                headers={"Retry-After": str(exc.retry_after)},
            )
            return
        except (ValueError, json.JSONDecodeError):
            self._send_json(
                {"error": "username and password are required"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        if session is None:
            self._send_json(
                {"error": "invalid username or password"}, HTTPStatus.UNAUTHORIZED
            )
            return
        self._send_json(
            {"authenticated": True, "username": session.username},
            headers={"Set-Cookie": self.server.authenticator.set_cookie_header(session)},
        )

    def _current_session(self) -> AuthSession | None:
        return self.server.authenticator.session_from_cookie(self.headers.get("Cookie"))

    def _require_auth(self, path: str) -> AuthSession | None:
        session = self._current_session()
        if session is not None:
            return session
        if path in {
            "/",
            "/index.html",
            "/camera",
            "/camera.html",
            "/recordings",
            "/recordings.html",
            "/settings",
            "/settings.html",
            "/system",
            "/system.html",
        }:
            self._redirect("/login")
        else:
            self._send_json(
                {"error": "authentication required"}, HTTPStatus.UNAUTHORIZED
            )
        return None

    def _require_csrf(self, session: AuthSession) -> bool:
        if self.server.authenticator.csrf_matches(
            session, self.headers.get("X-CSRF-Token")
        ):
            return True
        self._send_json({"error": "invalid CSRF token"}, HTTPStatus.FORBIDDEN)
        return False

    def _read_json(self) -> dict[str, object]:
        content_type = self.headers.get("Content-Type", "").partition(";")[0].lower()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length <= 0 or length > 16 * 1024:
            raise ValueError("a JSON request body is required")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    @staticmethod
    def _required_bool(payload: dict[str, object], key: str) -> bool:
        value = payload.get(key)
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value

    @staticmethod
    def _optional_bool(payload: dict[str, object], key: str) -> bool | None:
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value

    @staticmethod
    def _optional_number(payload: dict[str, object], key: str) -> float | None:
        value = payload.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        return float(value)

    @staticmethod
    def _optional_string_list(
        payload: dict[str, object], key: str
    ) -> list[str] | None:
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            raise ValueError(f"{key} must be an array of strings")
        return value

    def _base_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        if self.server.secure_transport:
            self.send_header(
                "Strict-Transport-Security", "max-age=31536000"
            )
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'",
        )

    def _send_json(
        self,
        payload: dict[str, object],
        status: HTTPStatus = HTTPStatus.OK,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self._base_headers("application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self._base_headers("text/plain; charset=utf-8")
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_static(
        self,
        filename: str,
        content_type: str,
        *,
        cache_public: bool,
        cache_private: bool = False,
    ) -> None:
        try:
            body = _read_static_file(filename)
        except OSError:
            self._send_json({"error": "asset unavailable"}, HTTPStatus.NOT_FOUND)
            return
        compressed = (
            content_type.startswith(("text/", "application/javascript"))
            and "gzip" in self.headers.get("Accept-Encoding", "").lower()
        )
        if compressed:
            body = _gzip_static_file(filename)
        self.send_response(HTTPStatus.OK)
        self._base_headers(content_type)
        if cache_public:
            cache_control = "public, max-age=3600"
        elif cache_private:
            cache_control = "private, max-age=3600"
        else:
            cache_control = "no-store"
        self.send_header("Cache-Control", cache_control)
        if compressed:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_snapshot(self) -> None:
        frame = self.server.stream.latest_frame()
        if frame is None:
            self._send_json(
                {"error": "camera has not produced a frame"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self.send_response(HTTPStatus.OK)
        self._base_headers("image/jpeg")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(frame)))
        self.end_headers()
        self.wfile.write(frame)

    def _serve_recording_download(
        self, recording_id: str, session: AuthSession
    ) -> None:
        try:
            path = self.server.stream.recordings.path(recording_id)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if path is None:
            self._send_json({"error": "recording not found"}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self._base_headers("application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with path.open("rb") as recording:
            while self.server.authenticator.is_active(session):
                chunk = recording.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _serve_recording(self, recording_id: str, session: AuthSession) -> None:
        try:
            path = self.server.stream.recordings.path(recording_id)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if path is None:
            self._send_json({"error": "recording not found"}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self._base_headers("multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        interval = 1.0 / self.server.stream.recordings.fps
        try:
            for frame in self.server.stream.recordings.frames(recording_id):
                if not self.server.authenticator.is_active(session):
                    break
                started = time.monotonic()
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                time.sleep(max(0.0, interval - (time.monotonic() - started)))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _serve_stream(self, session: AuthSession) -> None:
        self.send_response(HTTPStatus.OK)
        self._base_headers("multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()

        sequence = -1
        self.server.stream.add_client()
        try:
            while self.server.authenticator.is_active(session):
                frame, current_sequence = self.server.stream.wait_for_preview(sequence)
                if frame is None or current_sequence == sequence:
                    continue
                sequence = current_sequence
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            self.server.stream.remove_client()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and camera command, then exit",
    )
    parser.add_argument(
        "--hash-password",
        action="store_true",
        help="prompt for a password and print a salted verifier, then exit",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.hash_password:
        password = getpass.getpass("New dashboard password: ")
        confirmation = getpass.getpass("Confirm dashboard password: ")
        if password != confirmation:
            print("Passwords do not match", file=sys.stderr)
            return 2
        try:
            print(hash_password(password))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        config = Config.from_environment()
        tls_config = TLSConfig.from_environment(config.host)
        auth_config = AuthConfig.from_environment(
            secure_transport=tls_config.secure_transport
        )
        tls_context = tls_config.context()
    except ValueError as exc:
        LOG.error("Configuration is invalid: %s", exc)
        return 2

    try:
        command = config.capture_argv()
    except FileNotFoundError as exc:
        if args.check:
            LOG.error("Configuration check failed: %s", exc)
            return 1
        # Keep the web UI available with a useful offline state. The camera
        # supervisor periodically retries, so a package installed later is found.
        command = None

    if args.check:
        LOG.info("Configuration is valid: %s", asdict(config))
        LOG.info("Camera command: %s", shlex.join(command or []))
        return 0

    try:
        detection = DetectionEngine()
        defaults = DeviceSettings(
            camera_enabled=True,
            ai_enabled=detection.ai_enabled,
            motion_enabled=detection.motion_enabled,
            motion_sensitivity=detection.motion_sensitivity,
            ai_categories=detection.ai_categories,
        )
        settings_store = SettingsStore(
            os.getenv(
                "CAMERA_SETTINGS_FILE", str(ROOT / "state" / "device-settings.json")
            ),
            defaults,
        )
        saved_settings = settings_store.current
        detection.set_enabled(
            ai=saved_settings.ai_enabled,
            motion=saved_settings.motion_enabled,
            sensitivity=saved_settings.motion_sensitivity,
            categories=saved_settings.ai_categories,
        )
        LOG.info(
            "Restored device settings: camera=%s ai=%s categories=%s motion=%s "
            "sensitivity=%d",
            saved_settings.camera_enabled,
            saved_settings.ai_enabled,
            ",".join(saved_settings.ai_categories),
            saved_settings.motion_enabled,
            saved_settings.motion_sensitivity,
        )
    except ValueError as exc:
        LOG.error("Detection or saved settings configuration is invalid: %s", exc)
        return 2
    stream = CameraStream(
        config, detection, initial_enabled=saved_settings.camera_enabled
    )
    server = CameraHTTPServer(
        (config.host, config.port),
        stream,
        Authenticator(auth_config),
        settings_store=settings_store,
        secure_transport=tls_config.secure_transport,
    )
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    stopping = threading.Event()

    def request_shutdown(signum: int, _frame: object) -> None:
        if stopping.is_set():
            return
        stopping.set()
        stream.request_stop()
        LOG.info("Received signal %s; shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    detection.start()
    stream.start()
    scheme = "https" if tls_config.enabled else "http"
    LOG.info("Viewer available at %s://%s:%d", scheme, config.host, config.port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        stream.stop()
        detection.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
