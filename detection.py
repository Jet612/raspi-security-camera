"""Bounded motion detection with selectable Hailo or CPU object detection."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import Future
from functools import partial
from pathlib import Path
from typing import Any


LOG = logging.getLogger("camera.detection")

# COCO indices must match the class order embedded in the supplied YOLOv8 HEF.
COCO_CLASSES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep",
    "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
    "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
    "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
    "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv",
    "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
    "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
)

# Security-relevant classes only. Household objects and street furniture are
# intentionally excluded, even though the model can identify them.
CLASS_CATEGORIES = {
    "person": "person",
    "bicycle": "vehicle",
    "car": "vehicle",
    "motorcycle": "vehicle",
    "bus": "vehicle",
    "train": "vehicle",
    "truck": "vehicle",
    "bird": "animal",
    "cat": "animal",
    "dog": "animal",
    "horse": "animal",
    "sheep": "animal",
    "cow": "animal",
    "elephant": "animal",
    "bear": "animal",
    "zebra": "animal",
    "giraffe": "animal",
}

CATEGORY_THRESHOLDS = {"person": 0.45, "animal": 0.42, "vehicle": 0.50}


def _hailo_architecture() -> str | None:
    if not Path("/dev/hailo0").exists():
        return None
    try:
        result = subprocess.run(
            ["hailortcli", "fw-control", "identify"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        if "Device Architecture:" in line:
            return line.rsplit(":", 1)[-1].strip()
    return None


class _HailoModel:
    """Small synchronous wrapper over HailoRT's asynchronous Python API."""

    def __init__(self, model_path: str) -> None:
        import numpy as np
        from hailo_platform import (
            FormatType,
            HEF,
            HailoSchedulingAlgorithm,
            VDevice,
        )

        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
        self._np = np
        self._hef = HEF(model_path)
        self._device = VDevice(params)
        self._model = self._device.create_infer_model(model_path)
        self._model.set_batch_size(1)
        input_type = self._hef.get_input_vstream_infos()[0].format.type
        self._model.input().set_format_type(input_type)
        for output in self._model.outputs:
            output.set_format_type(FormatType.FLOAT32)
        self._configured = self._model.configure()

    def get_input_shape(self) -> tuple[int, int, int]:
        return self._hef.get_input_vstream_infos()[0].shape

    def run(self, frame: Any) -> Any:
        output_buffers = {
            name: self._np.empty(self._model.output(name).shape, dtype=self._np.float32)
            for name in self._model.output_names
        }
        bindings = self._configured.create_bindings(output_buffers=output_buffers)
        bindings.input().set_buffer(frame)
        future: Future[Any] = Future()

        def complete(completion_info: Any, *, bindings: Any) -> None:
            if completion_info.exception:
                future.set_exception(completion_info.exception)
            elif len(self._model.outputs) == 1:
                future.set_result(bindings.output().get_buffer())
            else:
                future.set_result(
                    {
                        name: bindings.output(name).get_buffer()
                        for name in self._model.output_names
                    }
                )

        self._configured.wait_for_async_ready(timeout_ms=10000)
        self._configured.run_async(
            [bindings], partial(complete, bindings=bindings)
        )
        return future.result(timeout=10)

    def close(self) -> None:
        del self._configured
        self._device.release()


class _OpenCVModel:
    """CPU object detection using YOLOv8 ONNX or OpenCV's built-in person model."""

    def __init__(self, cv2: Any, model_path: str | None, input_size: int) -> None:
        self._cv2 = cv2
        self._input_size = input_size
        self._net: Any = None
        self._hog: Any = None
        if model_path:
            if not Path(model_path).is_file():
                raise FileNotFoundError(f"CPU AI model not found: {model_path}")
            if Path(model_path).suffix.lower() != ".onnx":
                raise ValueError("CPU AI model must be a YOLOv8 .onnx file")
            self._net = cv2.dnn.readNetFromONNX(model_path)
            self.name = f"YOLOv8 · CPU · {Path(model_path).name}"
        else:
            self._hog = cv2.HOGDescriptor()
            self._hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
            self.name = "Person detector · CPU"

    def detect(self, image: Any) -> list[dict[str, object]]:
        if self._net is None:
            return self._detect_people(image)
        return self._detect_yolov8(image)

    def _detect_people(self, image: Any) -> list[dict[str, object]]:
        height, width = image.shape[:2]
        scale = min(1.0, 640.0 / max(width, height))
        sample = self._cv2.resize(image, None, fx=scale, fy=scale)
        boxes, weights = self._hog.detectMultiScale(
            sample, winStride=(8, 8), padding=(8, 8), scale=1.05
        )
        found: list[dict[str, object]] = []
        sample_h, sample_w = sample.shape[:2]
        for (x, y, box_w, box_h), weight in zip(boxes, weights):
            confidence = min(0.99, max(0.45, float(weight)))
            found.append(
                {
                    "label": "person",
                    "category": "person",
                    "confidence": round(confidence, 3),
                    "bbox": [
                        round(x / sample_w, 4),
                        round(y / sample_h, 4),
                        round((x + box_w) / sample_w, 4),
                        round((y + box_h) / sample_h, 4),
                    ],
                }
            )
        return sorted(found, key=lambda item: float(item["confidence"]), reverse=True)

    def _detect_yolov8(self, image: Any) -> list[dict[str, object]]:
        import numpy as np

        size = self._input_size
        blob = self._cv2.dnn.blobFromImage(
            image, 1.0 / 255.0, (size, size), swapRB=True, crop=False
        )
        self._net.setInput(blob)
        output = np.squeeze(self._net.forward())
        if output.ndim != 2:
            raise RuntimeError(f"unexpected YOLO output shape: {output.shape}")
        if output.shape[0] in {84, 85}:
            output = output.T

        boxes: list[list[float]] = []
        scores: list[float] = []
        class_ids: list[int] = []
        for row in output:
            class_scores = row[4:84]
            class_id = int(np.argmax(class_scores))
            confidence = float(class_scores[class_id])
            label = COCO_CLASSES[class_id]
            category = CLASS_CATEGORIES.get(label)
            if category is None or confidence < CATEGORY_THRESHOLDS[category]:
                continue
            center_x, center_y, width, height = (float(value) for value in row[:4])
            boxes.append(
                [center_x - width / 2, center_y - height / 2, width, height]
            )
            scores.append(confidence)
            class_ids.append(class_id)

        indices = self._cv2.dnn.NMSBoxes(boxes, scores, 0.4, 0.45)
        found: list[dict[str, object]] = []
        for index in indices:
            index = int(index)
            x, y, width, height = boxes[index]
            label = COCO_CLASSES[class_ids[index]]
            found.append(
                {
                    "label": label,
                    "category": CLASS_CATEGORIES[label],
                    "confidence": round(scores[index], 3),
                    "bbox": [
                        round(max(0.0, min(1.0, x / size)), 4),
                        round(max(0.0, min(1.0, y / size)), 4),
                        round(max(0.0, min(1.0, (x + width) / size)), 4),
                        round(max(0.0, min(1.0, (y + height) / size)), 4),
                    ],
                }
            )
        return sorted(found, key=lambda item: float(item["confidence"]), reverse=True)

    def close(self) -> None:
        self._net = None
        self._hog = None


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "1" if default else "0").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


class DetectionEngine:
    """Analyses at most the latest frame so inference can never delay video."""

    def __init__(self) -> None:
        self.motion_enabled = _env_bool("MOTION_ENABLED", True)
        self.ai_enabled = _env_bool("AI_ENABLED", True)
        self.analysis_fps = _env_float("DETECTION_FPS", 5.0, 0.5, 20.0)
        self.motion_threshold = _env_float(
            "MOTION_THRESHOLD", 0.012, 0.001, 0.5
        )
        self.motion_hold = _env_float("MOTION_HOLD_SECONDS", 3.0, 0.5, 30.0)
        self.ai_model_override = os.getenv("AI_MODEL")
        self.ai_backend = os.getenv("AI_BACKEND", "auto").strip().lower()
        if self.ai_backend not in {"auto", "hailo", "cpu"}:
            raise ValueError("AI_BACKEND must be auto, hailo, or cpu")
        self.cpu_model = os.getenv("AI_CPU_MODEL")
        self.cpu_input_size = int(_env_float("AI_INPUT_SIZE", 640, 160, 1280))

        self._condition = threading.Condition()
        self._pending_frame: bytes | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._worker, name="security-detection", daemon=True
        )

        self._motion_background: Any = None
        self._motion_until = 0.0
        self._motion_score = 0.0
        self._motion_warmup = 0
        self._detections: list[dict[str, object]] = []
        self._ai_state = "starting" if self.ai_enabled else "disabled"
        self._ai_error: str | None = None
        self._ai_model_name: str | None = None
        self._analysis_times: deque[float] = deque(maxlen=60)
        self._ai_backend_name: str | None = None
        self._model: Any = None
        self._next_ai_retry = 0.0
        self._hailo_failed_until = 0.0
        self._next_hailo_probe = 0.0

    @property
    def motion_sensitivity(self) -> int:
        # Map the useful changed-pixel range to a dashboard-friendly 1–100 scale.
        value = 100.0 - ((self.motion_threshold - 0.003) / (0.08 - 0.003) * 99.0)
        return round(max(1.0, min(100.0, value)))

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        self._thread.join(timeout=5)
        self._close_model()

    def set_enabled(
        self,
        *,
        ai: bool | None = None,
        motion: bool | None = None,
        sensitivity: float | None = None,
    ) -> dict[str, object]:
        """Apply dashboard detection settings without restarting the service."""
        with self._condition:
            if ai is not None and ai != self.ai_enabled:
                self.ai_enabled = ai
                self._detections = []
                self._ai_state = "starting" if ai else "disabled"
                self._ai_error = None
                if ai:
                    self._next_ai_retry = 0.0
            if motion is not None and motion != self.motion_enabled:
                self.motion_enabled = motion
                self._motion_until = 0.0
                self._motion_score = 0.0
                self._motion_background = None
                self._motion_warmup = 0
            if sensitivity is not None:
                if not 1 <= sensitivity <= 100:
                    raise ValueError("motion_sensitivity must be between 1 and 100")
                self.motion_threshold = 0.08 - ((sensitivity - 1) / 99 * 0.077)
                self._motion_background = None
                self._motion_warmup = 0
            self._condition.notify_all()
        return self.status()

    def submit(self, frame: bytes) -> None:
        with self._condition:
            self._pending_frame = frame
            self._condition.notify()

    def _worker(self) -> None:
        interval = 1.0 / self.analysis_fps
        last_analysis = 0.0
        cv2: Any = None
        np: Any = None
        while not self._stop.is_set():
            with self._condition:
                self._condition.wait_for(
                    lambda: self._stop.is_set()
                    or (
                        self._pending_frame is not None
                        and (self.motion_enabled or self.ai_enabled)
                    ),
                    timeout=1.0,
                )
                if self._stop.is_set():
                    break
                if not (self.motion_enabled or self.ai_enabled):
                    continue
                frame = self._pending_frame
                self._pending_frame = None

            if cv2 is None:
                try:
                    import cv2
                    import numpy as np
                except ImportError as exc:
                    LOG.error("Detection dependencies unavailable: %s", exc)
                    with self._condition:
                        self._ai_state = "unavailable"
                        self._ai_error = str(exc)
                    return

            now = time.monotonic()
            if now - last_analysis < interval or frame is None:
                continue
            last_analysis = now

            image = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                continue

            if self.motion_enabled:
                self._analyse_motion(image, cv2, np, now)
            if self.ai_enabled:
                self._analyse_objects(image, cv2, now)

            with self._condition:
                self._analysis_times.append(now)

    def _analyse_motion(self, image: Any, cv2: Any, np: Any, now: float) -> None:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (320, 180), interpolation=cv2.INTER_AREA)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        if self._motion_background is None:
            self._motion_background = gray.astype(np.float32)
            return

        background = cv2.convertScaleAbs(self._motion_background)
        difference = cv2.absdiff(gray, background)
        changed = cv2.threshold(difference, 25, 255, cv2.THRESH_BINARY)[1]
        changed = cv2.morphologyEx(changed, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        score = float(cv2.countNonZero(changed)) / float(changed.size)
        cv2.accumulateWeighted(gray, self._motion_background, 0.06)

        self._motion_warmup += 1
        # Very large changes are usually exposure transitions or a light being
        # switched. AI detection remains active during those transitions.
        is_motion = (
            self._motion_warmup >= 5
            and self.motion_threshold <= score <= 0.60
        )
        with self._condition:
            self._motion_score = score
            if is_motion:
                self._motion_until = now + self.motion_hold

    def _ensure_model(self, cv2: Any, now: float) -> bool:
        if self._model is not None:
            if (
                self.ai_backend == "auto"
                and self._ai_backend_name == "cpu"
                and now >= self._next_hailo_probe
            ):
                self._next_hailo_probe = now + 300.0
                if _hailo_architecture() in {"HAILO8", "HAILO8L"}:
                    LOG.info("Hailo module detected; upgrading AI backend from CPU")
                    self._close_model()
                else:
                    return True
            else:
                return True
        if now < self._next_ai_retry:
            return False

        self._next_ai_retry = now + 30.0
        try:
            architecture = None
            if self.ai_backend in {"auto", "hailo"}:
                architecture = _hailo_architecture()
            use_hailo = (
                architecture in {"HAILO8", "HAILO8L"}
                and (self.ai_backend == "hailo" or now >= self._hailo_failed_until)
            )
            if self.ai_backend == "hailo" and not use_hailo:
                raise RuntimeError("AI HAT+ is not detected on PCIe")

            if use_hailo and self.ai_backend != "cpu":
                if self.ai_model_override and self.ai_model_override.endswith(".hef"):
                    model_path = self.ai_model_override
                elif architecture == "HAILO8":
                    model_path = "/usr/share/hailo-models/yolov8s_h8.hef"
                else:
                    model_path = "/usr/share/hailo-models/yolov8s_h8l.hef"
                if not Path(model_path).is_file():
                    raise FileNotFoundError(f"AI model not found: {model_path}")
                self._model = _HailoModel(model_path)
                self._ai_model_name = f"YOLOv8s · {architecture}"
                self._ai_backend_name = "hailo"
            else:
                cpu_model = self.cpu_model
                if not cpu_model and self.ai_model_override and self.ai_model_override.endswith(".onnx"):
                    cpu_model = self.ai_model_override
                if not cpu_model:
                    bundled = Path(__file__).parent / "models" / "yolov8n.onnx"
                    cpu_model = str(bundled) if bundled.is_file() else None
                self._model = _OpenCVModel(cv2, cpu_model, self.cpu_input_size)
                self._ai_model_name = self._model.name
                self._ai_backend_name = "cpu"
                self._next_hailo_probe = now + 300.0
            with self._condition:
                self._ai_state = "online"
                self._ai_error = None
            LOG.info("AI detection online with %s", self._ai_model_name)
            return True
        except Exception as exc:  # HailoRT raises several hardware-specific types.
            if self.ai_backend == "auto" and use_hailo:
                hailo_message = str(exc) or exc.__class__.__name__
                self._hailo_failed_until = now + 300.0
                LOG.warning(
                    "Hailo backend unavailable (%s); falling back to CPU", hailo_message
                )
                try:
                    cpu_model = self.cpu_model
                    if not cpu_model and self.ai_model_override and self.ai_model_override.endswith(".onnx"):
                        cpu_model = self.ai_model_override
                    if not cpu_model:
                        bundled = Path(__file__).parent / "models" / "yolov8n.onnx"
                        cpu_model = str(bundled) if bundled.is_file() else None
                    self._model = _OpenCVModel(cv2, cpu_model, self.cpu_input_size)
                    self._ai_model_name = self._model.name
                    self._ai_backend_name = "cpu"
                    self._next_hailo_probe = now + 300.0
                    with self._condition:
                        self._ai_state = "online"
                        self._ai_error = None
                    return True
                except Exception as cpu_exc:
                    exc = cpu_exc
            message = str(exc) or exc.__class__.__name__
            with self._condition:
                self._ai_state = "unavailable"
                self._ai_error = message
                self._detections = []
            LOG.warning("AI detection unavailable: %s", message)
            return False

    def _analyse_objects(self, image: Any, cv2: Any, now: float) -> None:
        if not self._ensure_model(cv2, now):
            return
        try:
            if self._ai_backend_name == "hailo":
                model_h, model_w, _ = self._model.get_input_shape()
                resized = cv2.resize(image, (model_w, model_h), interpolation=cv2.INTER_AREA)
                model_input = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
                detections = self._extract_detections(self._model.run(model_input))
            else:
                detections = self._model.detect(image)
            with self._condition:
                self._detections = detections
                self._ai_state = "online"
                self._ai_error = None
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            LOG.error("AI inference failed: %s", message)
            failed_backend = self._ai_backend_name
            self._close_model()
            if failed_backend == "hailo" and self.ai_backend == "auto":
                self._hailo_failed_until = now + 300.0
            self._next_ai_retry = now + 10.0
            with self._condition:
                self._ai_state = "error"
                self._ai_error = message
                self._detections = []

    @staticmethod
    def _extract_detections(output: Any) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        for class_id, class_detections in enumerate(output):
            if class_id >= len(COCO_CLASSES):
                break
            label = COCO_CLASSES[class_id]
            category = CLASS_CATEGORIES.get(label)
            if category is None:
                continue
            for detection in class_detections:
                confidence = float(detection[4])
                if confidence < CATEGORY_THRESHOLDS[category]:
                    continue
                y0, x0, y1, x1 = (float(value) for value in detection[:4])
                bbox = [
                    round(max(0.0, min(1.0, x0)), 4),
                    round(max(0.0, min(1.0, y0)), 4),
                    round(max(0.0, min(1.0, x1)), 4),
                    round(max(0.0, min(1.0, y1)), 4),
                ]
                found.append(
                    {
                        "label": label,
                        "category": category,
                        "confidence": round(confidence, 3),
                        "bbox": bbox,
                    }
                )
        return sorted(found, key=lambda item: float(item["confidence"]), reverse=True)

    def _close_model(self) -> None:
        model, self._model = self._model, None
        self._ai_backend_name = None
        if model is not None:
            try:
                model.close()
            except Exception:
                LOG.exception("Failed to close AI inference backend")

    def status(self) -> dict[str, object]:
        now = time.monotonic()
        with self._condition:
            recent = [stamp for stamp in self._analysis_times if now - stamp <= 5.0]
            analysis_fps = 0.0
            if len(recent) > 1:
                analysis_fps = (len(recent) - 1) / (recent[-1] - recent[0])
            motion_active = self.motion_enabled and now < self._motion_until
            detections = list(self._detections)
            return {
                "active": motion_active or bool(detections),
                "motion": {
                    "enabled": self.motion_enabled,
                    "active": motion_active,
                    "score": round(self._motion_score, 4),
                    "sensitivity": self.motion_sensitivity,
                },
                "ai": {
                    "enabled": self.ai_enabled,
                    "online": self._ai_state == "online",
                    "state": self._ai_state,
                    "backend": self._ai_backend_name,
                    "model": self._ai_model_name,
                    "error": self._ai_error,
                },
                "analysis_fps": round(analysis_fps, 1),
                "detections": detections,
            }
