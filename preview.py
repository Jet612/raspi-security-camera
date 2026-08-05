"""Latest-frame live preview encoding that never blocks camera capture."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any


LOG = logging.getLogger("camera.preview")


class LivePreview:
    """Downscale one shared preview stream independently of source capture.

    Source frames are replaced rather than queued, so a slow encoder cannot add
    latency or delay high-quality snapshots, detection, or recording writes.
    """

    def __init__(
        self,
        *,
        width: int,
        height: int,
        fps: int,
        quality: int,
    ) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.quality = quality
        self._condition = threading.Condition()
        self._source_frame: bytes | None = None
        self._source_sequence = 0
        self._frame: bytes | None = None
        self._sequence = 0
        self._frame_times: deque[float] = deque(maxlen=max(60, fps * 6))
        self._output_size = (width, height)
        self._error: str | None = None
        self._passthrough = False
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._worker, name="live-preview", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def clear(self) -> None:
        with self._condition:
            self._source_frame = None
            self._frame = None
            self._frame_times.clear()
            self._source_sequence += 1
            self._sequence += 1
            self._condition.notify_all()

    def submit(self, frame: bytes) -> None:
        with self._condition:
            if self._passthrough:
                self._publish_locked(frame, time.monotonic(), None)
                return
            self._source_frame = frame
            self._source_sequence += 1
            self._condition.notify()

    def wait_for_frame(
        self, last_sequence: int, timeout: float = 10.0
    ) -> tuple[bytes | None, int]:
        with self._condition:
            self._condition.wait_for(
                lambda: self._sequence != last_sequence or self._stop.is_set(),
                timeout=timeout,
            )
            return self._frame, self._sequence

    def status(self) -> dict[str, object]:
        now = time.monotonic()
        with self._condition:
            recent = [stamp for stamp in self._frame_times if now - stamp <= 5.0]
            fps = 0.0
            if len(recent) > 1:
                fps = (len(recent) - 1) / (recent[-1] - recent[0])
            return {
                "fps": round(fps, 1),
                "resolution": f"{self._output_size[0]} × {self._output_size[1]}",
                "quality": self.quality,
                "passthrough": self._passthrough,
                "error": self._error,
            }

    def _worker(self) -> None:
        try:
            import cv2
            import numpy as np
        except (ImportError, OSError) as exc:
            self._enable_passthrough(f"preview encoder unavailable: {exc}")
            return

        interval = 1.0 / self.fps
        last_source_sequence = -1
        next_encode = 0.0
        while not self._stop.is_set():
            with self._condition:
                ready = self._condition.wait_for(
                    lambda: self._stop.is_set()
                    or (
                        self._source_frame is not None
                        and self._source_sequence != last_source_sequence
                    ),
                    timeout=1.0,
                )
                if self._stop.is_set():
                    break
                if not ready:
                    continue

            delay = next_encode - time.monotonic()
            if delay > 0 and self._stop.wait(delay):
                break

            # Pick up the newest source frame after throttling, not the older
            # frame that originally woke the worker.
            with self._condition:
                frame = self._source_frame
                last_source_sequence = self._source_sequence
            if frame is None:
                continue

            try:
                encoded, output_size = self._encode(frame, cv2, np)
            except Exception as exc:  # Native OpenCV errors vary by platform.
                self._enable_passthrough(f"preview encoding failed: {exc}")
                return

            now = time.monotonic()
            with self._condition:
                self._publish_locked(encoded, now, output_size)
            next_encode = now + interval

    def _encode(self, frame: bytes, cv2: Any, np: Any) -> tuple[bytes, tuple[int, int]]:
        image = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("camera returned an invalid JPEG")

        source_height, source_width = image.shape[:2]
        scale = min(self.width / source_width, self.height / source_height, 1.0)
        output_width = max(1, round(source_width * scale))
        output_height = max(1, round(source_height * scale))
        if (output_width, output_height) != (source_width, source_height):
            image = cv2.resize(
                image,
                (output_width, output_height),
                interpolation=cv2.INTER_AREA,
            )
        successful, encoded = cv2.imencode(
            ".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, self.quality]
        )
        if not successful:
            raise RuntimeError("OpenCV could not encode the live preview")
        return encoded.tobytes(), (output_width, output_height)

    def _enable_passthrough(self, message: str) -> None:
        LOG.warning("%s; using full-quality frames for live viewing", message)
        with self._condition:
            self._passthrough = True
            self._error = message
            if self._source_frame is not None:
                self._publish_locked(self._source_frame, time.monotonic(), None)
            self._condition.notify_all()

    def _publish_locked(
        self,
        frame: bytes,
        now: float,
        output_size: tuple[int, int] | None,
    ) -> None:
        self._frame = frame
        if output_size is not None:
            self._output_size = output_size
        self._sequence += 1
        self._frame_times.append(now)
        self._condition.notify_all()
