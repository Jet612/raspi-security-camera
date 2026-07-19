#!/usr/bin/env python3
"""Dependency-free MJPEG web server for a Raspberry Pi camera."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from detection import DetectionEngine
from recording import RecordingManager


LOG = logging.getLogger("camera")
ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"
JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"
MAX_JPEG_BYTES = 32 * 1024 * 1024


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


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

    @classmethod
    def from_environment(cls) -> "Config":
        autofocus_mode = os.getenv("CAMERA_AF_MODE", "continuous")
        if autofocus_mode not in {"default", "manual", "auto", "continuous"}:
            raise ValueError(
                "CAMERA_AF_MODE must be default, manual, auto, or continuous"
            )
        return cls(
            host=os.getenv("CAMERA_HOST", "0.0.0.0"),
            port=env_int("CAMERA_PORT", 8080, 1, 65535),
            width=env_int("CAMERA_WIDTH", 1920, 320, 4608),
            height=env_int("CAMERA_HEIGHT", 1080, 240, 2592),
            framerate=env_int("CAMERA_FPS", 20, 1, 60),
            quality=env_int("CAMERA_QUALITY", 75, 1, 100),
            sensor_mode=os.getenv("CAMERA_SENSOR_MODE", "2304:1296:10:P"),
            autofocus_mode=autofocus_mode,
            camera_command=os.getenv("CAMERA_COMMAND") or None,
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


class CameraStream:
    """Owns one capture process and publishes only its most recent JPEG."""

    def __init__(
        self,
        config: Config,
        detection: DetectionEngine | None = None,
        recordings: RecordingManager | None = None,
    ) -> None:
        self.config = config
        self.detection = detection
        self.recordings = recordings or RecordingManager(fps=config.framerate)
        self._condition = threading.Condition()
        self._frame: bytes | None = None
        self._sequence = 0
        self._frame_at = 0.0
        self._frame_times: deque[float] = deque(maxlen=max(120, config.framerate * 6))
        self._clients = 0
        self._state = "starting"
        self._error: str | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._enabled = True
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(
            target=self._supervise, name="camera-capture", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._condition:
            self._condition.notify_all()
        process = self._process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        self._thread.join(timeout=5)
        self.recordings.close()

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
            self._condition.notify_all()
        if self.detection is not None:
            self.detection.submit(frame)
        self.recordings.write(frame)

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

    def add_client(self) -> None:
        with self._condition:
            self._clients += 1

    def remove_client(self) -> None:
        with self._condition:
            self._clients = max(0, self._clients - 1)

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
        return status


class CameraHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], stream: CameraStream) -> None:
        self.stream = stream
        super().__init__(address, CameraRequestHandler)


class CameraRequestHandler(BaseHTTPRequestHandler):
    server: CameraHTTPServer
    protocol_version = "HTTP/1.1"
    static_files = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/index.html": ("index.html", "text/html; charset=utf-8"),
        "/styles.css": ("styles.css", "text/css; charset=utf-8"),
        "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    }

    def log_message(self, fmt: str, *args: object) -> None:
        LOG.info("%s - %s", self.client_address[0], fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/stream.mjpg":
            self._serve_stream()
        elif path == "/api/status":
            self._send_json(self.server.stream.status())
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
            self._serve_recording(recording_id)
        elif path.startswith("/api/recordings/") and path.endswith("/download"):
            recording_id = path.split("/")[3]
            self._serve_recording_download(recording_id)
        elif path in self.static_files:
            self._serve_static(*self.static_files[path])
        else:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            if path == "/api/camera":
                payload = self._read_json()
                enabled = self._required_bool(payload, "enabled")
                self._send_json(self.server.stream.set_enabled(enabled))
            elif path == "/api/detection":
                payload = self._read_json()
                ai = self._optional_bool(payload, "ai_enabled")
                motion = self._optional_bool(payload, "motion_enabled")
                sensitivity = self._optional_number(payload, "motion_sensitivity")
                if ai is None and motion is None and sensitivity is None:
                    raise ValueError(
                        "ai_enabled, motion_enabled, or motion_sensitivity is required"
                    )
                detection = self.server.stream.detection
                if detection is None:
                    self._send_json(
                        {"error": "detection is unavailable"},
                        HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                    return
                self._send_json(
                    detection.set_enabled(
                        ai=ai, motion=motion, sensitivity=sensitivity
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
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except OSError as exc:
            LOG.exception("Device control failed")
            self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
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

    def _read_json(self) -> dict[str, object]:
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

    def _base_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'",
        )

    def _send_json(
        self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK
    ) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self._base_headers("application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, filename: str, content_type: str) -> None:
        try:
            body = (STATIC_ROOT / filename).read_bytes()
        except OSError:
            self._send_json({"error": "asset unavailable"}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self._base_headers(content_type)
        self.send_header("Cache-Control", "public, max-age=3600")
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

    def _serve_recording_download(self, recording_id: str) -> None:
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
            shutil.copyfileobj(recording, self.wfile)

    def _serve_recording(self, recording_id: str) -> None:
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
                started = time.monotonic()
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                time.sleep(max(0.0, interval - (time.monotonic() - started)))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _serve_stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self._base_headers("multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()

        sequence = -1
        self.server.stream.add_client()
        try:
            while True:
                frame, current_sequence = self.server.stream.wait_for_frame(sequence)
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        config = Config.from_environment()
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
    except ValueError as exc:
        LOG.error("Detection configuration is invalid: %s", exc)
        return 2
    stream = CameraStream(config, detection)
    server = CameraHTTPServer((config.host, config.port), stream)
    stopping = threading.Event()

    def request_shutdown(signum: int, _frame: object) -> None:
        if stopping.is_set():
            return
        stopping.set()
        LOG.info("Received signal %s; shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    detection.start()
    stream.start()
    LOG.info("Viewer available at http://%s:%d", config.host, config.port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        stream.stop()
        detection.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
