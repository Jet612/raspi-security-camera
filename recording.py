"""Thread-safe local MJPEG recording storage and playback helpers."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator


LOG = logging.getLogger("camera.recording")
JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"
RECORDING_ID = re.compile(r"^\d{8}T\d{6}(?:-\d+)?$")


class RecordingManager:
    """Writes the shared camera frames to disk without starting another camera."""

    def __init__(self, directory: str | Path | None = None, fps: int = 20) -> None:
        configured = directory or os.getenv("RECORDINGS_DIR")
        self.directory = Path(configured) if configured else Path(__file__).parent / "recordings"
        self.fps = fps
        self._lock = threading.Lock()
        self._file: BinaryIO | None = None
        self._recording_id: str | None = None
        self._started_at: datetime | None = None
        self._frames = 0
        self._bytes = 0

    def start(self) -> dict[str, object]:
        with self._lock:
            if self._file is not None:
                return self._active_status_locked()
            self.directory.mkdir(parents=True, exist_ok=True)
            started_at = datetime.now(timezone.utc)
            base = started_at.strftime("%Y%m%dT%H%M%S")
            recording_id = base
            suffix = 2
            while self._video_path(recording_id).exists():
                recording_id = f"{base}-{suffix}"
                suffix += 1
            self._file = self._video_path(recording_id).open("xb")
            self._recording_id = recording_id
            self._started_at = started_at
            self._frames = 0
            self._bytes = 0
            LOG.info("Recording started: %s", recording_id)
            return self._active_status_locked()

    def stop(self) -> dict[str, object] | None:
        with self._lock:
            if self._file is None or self._recording_id is None:
                return None
            recording = self._active_status_locked()
            file_handle, self._file = self._file, None
            file_handle.flush()
            file_handle.close()
            self._write_metadata_locked(recording)
            LOG.info("Recording stopped: %s", self._recording_id)
            self._recording_id = None
            self._started_at = None
            self._frames = 0
            self._bytes = 0
            return recording

    def close(self) -> None:
        self.stop()

    def write(self, frame: bytes) -> None:
        with self._lock:
            if self._file is None:
                return
            try:
                self._file.write(frame)
                self._frames += 1
                self._bytes += len(frame)
            except OSError as exc:
                LOG.error("Recording write failed: %s", exc)
                file_handle, self._file = self._file, None
                file_handle.close()
                self._recording_id = None
                self._started_at = None

    def status(self) -> dict[str, object]:
        with self._lock:
            if self._file is None:
                return {"active": False, "id": None, "duration_seconds": 0}
            return self._active_status_locked()

    def list(self) -> list[dict[str, object]]:
        if not self.directory.exists():
            return []
        active = self.status()
        recordings: list[dict[str, object]] = []
        for video_path in self.directory.glob("*.mjpeg"):
            recording_id = video_path.stem
            if not RECORDING_ID.fullmatch(recording_id):
                continue
            file_metadata = video_path.stat()
            metadata = self._read_metadata(recording_id)
            if metadata is None:
                stamp = datetime.fromtimestamp(file_metadata.st_mtime, timezone.utc)
                metadata = {
                    "id": recording_id,
                    "started_at": stamp.isoformat(),
                    "duration_seconds": 0,
                    "frames": 0,
                }
            metadata["bytes"] = file_metadata.st_size
            metadata["active"] = active.get("id") == recording_id
            if metadata["active"]:
                metadata.update(active)
            recordings.append(metadata)
        return sorted(recordings, key=lambda item: str(item["started_at"]), reverse=True)

    def delete(self, recording_id: str) -> bool:
        self._validate_id(recording_id)
        with self._lock:
            if recording_id == self._recording_id:
                raise RuntimeError("stop the active recording before deleting it")
            video = self._video_path(recording_id)
            if not video.exists():
                return False
            video.unlink()
            metadata = self._metadata_path(recording_id)
            if metadata.exists():
                metadata.unlink()
            return True

    def path(self, recording_id: str) -> Path | None:
        self._validate_id(recording_id)
        path = self._video_path(recording_id)
        return path if path.is_file() else None

    def frames(self, recording_id: str) -> Iterator[bytes]:
        path = self.path(recording_id)
        if path is None:
            return
        buffer = bytearray()
        with path.open("rb") as recording:
            while chunk := recording.read(256 * 1024):
                buffer.extend(chunk)
                while True:
                    start = buffer.find(JPEG_START)
                    if start < 0:
                        buffer.clear()
                        break
                    end = buffer.find(JPEG_END, start + 2)
                    if end < 0:
                        if start:
                            del buffer[:start]
                        break
                    end += len(JPEG_END)
                    yield bytes(buffer[start:end])
                    del buffer[:end]

    def _active_status_locked(self) -> dict[str, object]:
        duration = 0.0
        if self._started_at is not None:
            duration = (datetime.now(timezone.utc) - self._started_at).total_seconds()
        return {
            "active": True,
            "id": self._recording_id,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "duration_seconds": round(max(0.0, duration), 1),
            "frames": self._frames,
            "bytes": self._bytes,
        }

    def _write_metadata_locked(self, recording: dict[str, object]) -> None:
        path = self._metadata_path(str(recording["id"]))
        try:
            path.write_text(json.dumps(recording, separators=(",", ":")))
        except OSError:
            LOG.exception("Could not write recording metadata for %s", recording["id"])

    def _read_metadata(self, recording_id: str) -> dict[str, object] | None:
        try:
            value = json.loads(self._metadata_path(recording_id).read_text())
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def _video_path(self, recording_id: str) -> Path:
        return self.directory / f"{recording_id}.mjpeg"

    def _metadata_path(self, recording_id: str) -> Path:
        return self.directory / f"{recording_id}.json"

    @staticmethod
    def _validate_id(recording_id: str) -> None:
        if not RECORDING_ID.fullmatch(recording_id):
            raise ValueError("invalid recording id")
