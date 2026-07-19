import builtins
import os
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from detection import DetectionEngine


class DetectionFilterTests(unittest.TestCase):
    def test_only_security_relevant_classes_are_returned(self):
        output = [[] for _ in range(80)]
        output[0] = [[0.1, 0.2, 0.8, 0.7, 0.91]]   # person
        output[15] = [[0.2, 0.3, 0.6, 0.8, 0.84]]  # cat
        output[56] = [[0.1, 0.1, 0.5, 0.5, 0.99]]  # chair: ignored

        detections = DetectionEngine._extract_detections(output)

        self.assertEqual([item["label"] for item in detections], ["person", "cat"])
        self.assertEqual(detections[0]["category"], "person")
        self.assertEqual(detections[1]["category"], "animal")

    def test_category_specific_confidence_thresholds(self):
        output = [[] for _ in range(80)]
        output[0] = [[0, 0, 1, 1, 0.44]]   # person threshold is 0.45
        output[16] = [[0, 0, 1, 1, 0.43]]  # dog threshold is 0.42
        output[2] = [[0, 0, 1, 1, 0.49]]   # car threshold is 0.50

        detections = DetectionEngine._extract_detections(output)

        self.assertEqual([item["label"] for item in detections], ["dog"])

    def test_detection_can_be_disabled_by_environment(self):
        with patch.dict(
            os.environ,
            {"MOTION_ENABLED": "false", "AI_ENABLED": "false"},
            clear=True,
        ):
            engine = DetectionEngine()
        status = engine.status()
        self.assertFalse(status["motion"]["enabled"])
        self.assertEqual(status["ai"]["state"], "disabled")

    def test_disabled_worker_does_not_import_native_dependencies(self):
        with patch.dict(
            os.environ,
            {"MOTION_ENABLED": "false", "AI_ENABLED": "false"},
            clear=True,
        ):
            engine = DetectionEngine()
        real_import = builtins.__import__
        import_attempted = threading.Event()

        def track_import(name, *args, **kwargs):
            if name in {"cv2", "numpy"}:
                import_attempted.set()
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=track_import):
            engine.start()
            try:
                engine.submit(b"unused-frame")
                self.assertFalse(import_attempted.wait(timeout=0.1))
            finally:
                engine.stop()

    def test_motion_detector_ignores_warmup_then_detects_change(self):
        import cv2

        with patch.dict(os.environ, {"AI_ENABLED": "false"}, clear=True):
            engine = DetectionEngine()
        quiet = np.zeros((360, 640, 3), dtype=np.uint8)
        now = time.monotonic()
        for offset in range(6):
            engine._analyse_motion(quiet, cv2, np, now + offset * 0.2)
        self.assertFalse(engine.status()["motion"]["active"])

        changed = quiet.copy()
        changed[80:280, 180:460] = 255
        engine._analyse_motion(changed, cv2, np, time.monotonic())
        self.assertTrue(engine.status()["motion"]["active"])

    def test_motion_sensitivity_can_be_changed_at_runtime(self):
        with patch.dict(os.environ, {"AI_ENABLED": "false"}, clear=True):
            engine = DetectionEngine()
        original_threshold = engine.motion_threshold

        status = engine.set_enabled(sensitivity=100)

        self.assertEqual(status["motion"]["sensitivity"], 100)
        self.assertLess(engine.motion_threshold, original_threshold)
        with self.assertRaisesRegex(ValueError, "between 1 and 100"):
            engine.set_enabled(sensitivity=101)

    def test_auto_backend_defaults_to_cpu_without_hailo(self):
        import cv2

        with patch.dict(os.environ, {}, clear=True):
            engine = DetectionEngine()
        with patch("detection._hailo_architecture", return_value=None):
            self.assertTrue(engine._ensure_model(cv2, time.monotonic()))
        self.assertEqual(engine.status()["ai"]["backend"], "cpu")
        engine._close_model()


if __name__ == "__main__":
    unittest.main()
