"""Thread-safe camera recording storage and MP4 conversion helpers."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator


LOG = logging.getLogger("camera.recording")
JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"
RECORDING_ID = re.compile(r"^\d{8}T\d{6}(?:-\d+)?$")
_AUTO_FFMPEG = object()


class RecordingManager:
    """Writes the shared camera frames to disk without starting another camera."""

    def __init__(
        self,
        directory: str | Path | None = None,
        fps: int = 20,
        ffmpeg_binary: str | None | object = _AUTO_FFMPEG,
    ) -> None:
        configured = directory or os.getenv("RECORDINGS_DIR")
        self.directory = (
            Path(configured)
            if configured
            else Path(__file__).parent / "recordings"
        )
        self.fps = fps
        self.ffmpeg_binary = (
            shutil.which(os.getenv("FFMPEG_BINARY", "ffmpeg"))
            if ffmpeg_binary is _AUTO_FFMPEG
            else ffmpeg_binary
        )
        self._lock = threading.Lock()
        self._file: BinaryIO | None = None
        self._recording_id: str | None = None
        self._started_at: datetime | None = None
        self._frames = 0
        self._bytes = 0
        self._processing: set[str] = set()
        self._conversion_queue: queue.Queue[str] = queue.Queue()
        self._converter_thread: threading.Thread | None = None
        if self.ffmpeg_binary:
            self._converter_thread = threading.Thread(
                target=self._conversion_worker,
                name="recording-mp4-converter",
                daemon=True,
            )
            self._converter_thread.start()
            self._recover_legacy_recordings()

    def start(self) -> dict[str, object]:
        with self._lock:
            if self._file is not None:
                return self._active_status_locked()
            self.directory.mkdir(parents=True, exist_ok=True)
            started_at = datetime.now(timezone.utc)
            base = started_at.strftime("%Y%m%dT%H%M%S")
            recording_id = base
            suffix = 2
            while self._recording_exists(recording_id):
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
            recording_id = self._recording_id
            recording["duration_seconds"] = round(self._frames / self.fps, 1)
            recording["format"] = "mjpeg"
            recording["processing"] = bool(self.ffmpeg_binary)
            file_handle, self._file = self._file, None
            file_handle.flush()
            file_handle.close()
            if not self.ffmpeg_binary:
                recording["conversion_error"] = (
                    "FFmpeg is unavailable; rerun the installer to enable MP4 recordings."
                )
            self._write_metadata_locked(recording)
            LOG.info("Recording stopped: %s", recording_id)
            self._recording_id = None
            self._started_at = None
            self._frames = 0
            self._bytes = 0
        if self.ffmpeg_binary:
            self._enqueue_conversion(recording_id)
        return recording

    def close(self) -> None:
        self.stop()

    def set_fps(self, fps: int) -> None:
        """Update metadata/playback rate only while no recording is active."""
        if isinstance(fps, bool) or not isinstance(fps, int) or not 1 <= fps <= 60:
            raise ValueError("recording fps must be an integer between 1 and 60")
        with self._lock:
            if self._file is not None:
                raise RuntimeError(
                    "stop the active recording before changing recording quality"
                )
            self.fps = fps

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
        with self._lock:
            processing = set(self._processing)
        recordings: list[dict[str, object]] = []
        recording_ids = {
            path.stem
            for pattern in ("*.mjpeg", "*.mp4")
            for path in self.directory.glob(pattern)
        }
        for recording_id in recording_ids:
            if not RECORDING_ID.fullmatch(recording_id):
                continue
            video_path = self._preferred_path(recording_id)
            if video_path is None:
                continue
            try:
                file_metadata = video_path.stat()
            except FileNotFoundError:
                video_path = self._preferred_path(recording_id)
                if video_path is None:
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
            metadata["format"] = "mp4" if video_path.suffix == ".mp4" else "mjpeg"
            metadata["processing"] = recording_id in processing
            metadata["playback_ready"] = (
                not metadata["active"] and not metadata["processing"]
            )
            if metadata["active"]:
                metadata.update(active)
            recordings.append(metadata)
        return sorted(recordings, key=lambda item: str(item["started_at"]), reverse=True)

    def delete(self, recording_id: str) -> bool:
        self._validate_id(recording_id)
        with self._lock:
            if recording_id == self._recording_id:
                raise RuntimeError("stop the active recording before deleting it")
            if recording_id in self._processing:
                raise RuntimeError("wait for MP4 preparation to finish before deleting it")
            videos = (self._mjpeg_path(recording_id), self._mp4_path(recording_id))
            found = any(video.exists() for video in videos)
            if not found:
                return False
            for video in videos:
                if video.exists():
                    video.unlink()
            metadata = self._metadata_path(recording_id)
            if metadata.exists():
                metadata.unlink()
            return True

    def path(self, recording_id: str) -> Path | None:
        self._validate_id(recording_id)
        return self._preferred_path(recording_id)

    def mp4_path(self, recording_id: str) -> Path | None:
        self._validate_id(recording_id)
        path = self._mp4_path(recording_id)
        return path if path.is_file() else None

    def frames(self, recording_id: str) -> Iterator[bytes]:
        self._validate_id(recording_id)
        path = self._mjpeg_path(recording_id)
        if not path.is_file():
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

    def recording_fps(self, recording_id: str) -> int:
        self._validate_id(recording_id)
        metadata = self._read_metadata(recording_id) or {}
        value = metadata.get("fps", self.fps)
        return value if isinstance(value, int) and 1 <= value <= 60 else self.fps

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
            "fps": self.fps,
        }

    def _write_metadata_locked(self, recording: dict[str, object]) -> None:
        path = self._metadata_path(str(recording["id"]))
        temporary = path.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(recording, separators=(",", ":")))
            os.replace(temporary, path)
        except OSError:
            LOG.exception("Could not write recording metadata for %s", recording["id"])
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _read_metadata(self, recording_id: str) -> dict[str, object] | None:
        try:
            value = json.loads(self._metadata_path(recording_id).read_text())
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def _video_path(self, recording_id: str) -> Path:
        """Return the legacy raw path retained for compatibility."""
        return self._mjpeg_path(recording_id)

    def _mjpeg_path(self, recording_id: str) -> Path:
        return self.directory / f"{recording_id}.mjpeg"

    def _mp4_path(self, recording_id: str) -> Path:
        return self.directory / f"{recording_id}.mp4"

    def _metadata_path(self, recording_id: str) -> Path:
        return self.directory / f"{recording_id}.json"

    def _preferred_path(self, recording_id: str) -> Path | None:
        for path in (self._mp4_path(recording_id), self._mjpeg_path(recording_id)):
            if path.is_file():
                return path
        return None

    def _recording_exists(self, recording_id: str) -> bool:
        return any(
            path.exists()
            for path in (
                self._mjpeg_path(recording_id),
                self._mp4_path(recording_id),
                self._metadata_path(recording_id),
            )
        )

    def _recover_legacy_recordings(self) -> None:
        if not self.directory.exists():
            return
        for path in sorted(self.directory.glob("*.mjpeg")):
            if not RECORDING_ID.fullmatch(path.stem):
                continue
            if self._mp4_path(path.stem).is_file():
                try:
                    path.unlink()
                except OSError:
                    LOG.exception("Could not remove converted MJPEG copy: %s", path.stem)
                continue
            self._enqueue_conversion(path.stem)

    def _enqueue_conversion(self, recording_id: str) -> None:
        if not self.ffmpeg_binary or not self._mjpeg_path(recording_id).is_file():
            return
        with self._lock:
            if (
                recording_id in self._processing
                or self._mp4_path(recording_id).is_file()
            ):
                return
            self._processing.add(recording_id)
        self._conversion_queue.put(recording_id)

    def _conversion_worker(self) -> None:
        while True:
            recording_id = self._conversion_queue.get()
            try:
                self._convert_recording(recording_id)
            finally:
                with self._lock:
                    self._processing.discard(recording_id)
                self._conversion_queue.task_done()

    def _convert_recording(self, recording_id: str) -> None:
        source = self._mjpeg_path(recording_id)
        output = self._mp4_path(recording_id)
        temporary = self.directory / f".{recording_id}.mp4.part"
        if not source.is_file() or output.is_file() or not self.ffmpeg_binary:
            return
        recording_fps = self.recording_fps(recording_id)
        command = [
            str(self.ffmpeg_binary),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "mjpeg",
            "-framerate",
            str(recording_fps),
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            "pad=ceil(iw/2)*2:ceil(ih/2)*2,"
            "scale=in_range=full:out_range=limited,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-threads",
            "2",
            "-f",
            "mp4",
            str(temporary),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise RuntimeError("FFmpeg did not create an MP4 file")
            os.replace(temporary, output)
            source.unlink()
            with self._lock:
                metadata = self._read_metadata(recording_id) or {
                    "id": recording_id,
                    "started_at": datetime.fromtimestamp(
                        output.stat().st_mtime, timezone.utc
                    ).isoformat(),
                    "duration_seconds": 0,
                    "frames": 0,
                }
                metadata.update(
                    {
                        "bytes": output.stat().st_size,
                        "format": "mp4",
                        "processing": False,
                    }
                )
                metadata.pop("conversion_error", None)
                self._write_metadata_locked(metadata)
            LOG.info("MP4 recording ready: %s", recording_id)
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            LOG.error("Could not convert recording %s to MP4: %s", recording_id, exc)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            with self._lock:
                metadata = self._read_metadata(recording_id) or {"id": recording_id}
                metadata.update(
                    {
                        "format": "mjpeg",
                        "processing": False,
                        "conversion_error": (
                            "MP4 conversion failed; rerun the installer to repair "
                            "FFmpeg."
                        ),
                    }
                )
                self._write_metadata_locked(metadata)

    @staticmethod
    def _validate_id(recording_id: str) -> None:
        if not RECORDING_ID.fullmatch(recording_id):
            raise ValueError("invalid recording id")
