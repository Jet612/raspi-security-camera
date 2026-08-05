import unittest

from preview import LivePreview


class LivePreviewTests(unittest.TestCase):
    def test_preview_downscales_and_recompresses_without_changing_source(self):
        try:
            import cv2
            import numpy as np
        except (ImportError, OSError) as exc:
            self.skipTest(f"OpenCV preview dependencies unavailable: {exc}")

        random = np.random.default_rng(42)
        image = random.integers(0, 256, (360, 640, 3), dtype=np.uint8)
        successful, encoded = cv2.imencode(
            ".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90]
        )
        self.assertTrue(successful)
        source = encoded.tobytes()
        preview = LivePreview(width=320, height=180, fps=10, quality=45)

        output, dimensions = preview._encode(source, cv2, np)

        decoded = cv2.imdecode(np.frombuffer(output, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(dimensions, (320, 180))
        self.assertEqual(decoded.shape[:2], (180, 320))
        self.assertLess(len(output), len(source))
        self.assertEqual(encoded.tobytes(), source)

    def test_passthrough_keeps_live_stream_available_on_encoder_failure(self):
        preview = LivePreview(width=320, height=180, fps=10, quality=45)
        preview._source_frame = b"high-quality-source"

        preview._enable_passthrough("test failure")
        frame, sequence = preview.wait_for_frame(-1, timeout=0)

        self.assertEqual(frame, b"high-quality-source")
        self.assertGreaterEqual(sequence, 1)
        self.assertTrue(preview.status()["passthrough"])


if __name__ == "__main__":
    unittest.main()
