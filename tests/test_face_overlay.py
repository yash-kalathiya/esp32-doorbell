import asyncio
import time

import httpx
import numpy as np
import pytest

from server.app import CameraHub, Settings
from server.faces import OpenCVEngine


@pytest.mark.asyncio
async def test_overlay_labels_quality_no_image_leak_and_expiry(tmp_path):
    vector = np.zeros(128, dtype=np.float32); vector[0] = 1
    face = {"embedding": vector, "thumbnail": b"private-thumbnail", "quality": 90, "box": [10, 20, 80, 90]}
    class Engine:
        def detect(self, jpeg):
            return {"faces": [face], "rejected": 1, "image_width": 320, "image_height": 240,
                    "rejected_boxes": [{"box": [200, 20, 20, 25], "reason": "too_small"},
                                       {"box": [200, 80, 40, 50], "reason": "uncertain"}]}
    def forbidden(request):
        pytest.fail("Overlay made another camera HTTP request while live frames existed")
    hub = CameraHub(tmp_path, httpx.MockTransport(forbidden), face_engine=Engine())
    hub.faces.enroll_photos("Overlay Test", [face])
    hub.online = True; hub.status = {"boot_id": "test-boot"}
    hub.frame = b"jpeg-test"; hub.frame_id = 1; hub.last_frame_at = time.monotonic()
    hub.producer = asyncio.create_task(asyncio.sleep(60))
    hub.face_task = asyncio.create_task(hub.recognize_faces())
    try:
        async with asyncio.timeout(2):
            while hub.face_overlay is None:
                await asyncio.sleep(.01)
        data = hub.face_status()["live_detections"]
        assert data["image_width"] == 320 and data["faces"][0]["label"] == "Overlay Test"
        assert data["faces"][0]["matched"] and data["faces"][1]["label"] == "Move closer"
        assert data["faces"][2]["label"] == "Face candidate" and not data["faces"][2]["matched"]
        assert not any("embedding" in face or "thumbnail" in face for face in data["faces"])
        hub.face_overlay["captured_at"] = time.monotonic() - 6
        assert hub.face_status()["live_detections"] is None
        hub.face_overlay["captured_at"] = time.monotonic()
        hub.status["boot_id"] = "new-boot"
        assert hub.face_status()["live_detections"] is None
        hub.status["boot_id"] = "test-boot"
        await hub.configure(Settings(face_monitoring=False))
        assert hub.face_status()["live_detections"] is None
    finally:
        await hub.close()


def test_rejected_faces_keep_boxes_without_weakening_recognition():
    import cv2
    class Detector:
        def setInputSize(self, size): pass
        def detect(self, image):
            return None, np.array([[10, 20, 20, 24] + [0] * 10 + [.99],
                                   [80, 60, 80, 90] + [0] * 10 + [.98],
                                   [80, 1, 60, 50] + [0] * 10 + [.8]], dtype=np.float32)
    engine = object.__new__(OpenCVEngine); engine.cv = cv2; engine.detector = Detector()
    _, jpeg = cv2.imencode('.jpg', np.zeros((192, 192, 3), dtype=np.uint8))
    result = engine.detect(jpeg.tobytes())
    assert not result["faces"] and result["rejected"] == 3
    assert result["image_width"] == 192 and result["image_height"] == 192
    assert {box["reason"] for box in result["rejected_boxes"]} == {"too_small", "blurry", "uncertain"}
