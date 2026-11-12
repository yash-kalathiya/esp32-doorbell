"""Burst storage/protocol tests use synthetic faces; rotation tests use a public sample."""
from io import BytesIO
from pathlib import Path
import time

from fastapi.testclient import TestClient
import httpx
import numpy as np
from PIL import Image
import pytest

from server.app import CameraHub, create_app
from server.faces import OpenCVEngine, restore_box


def photo(red=200):
    out = BytesIO()
    Image.new("RGB", (128, 128), (red, 0, 0)).save(out, "JPEG")
    return out.getvalue()


def face(red=200):
    vector = np.zeros(128, dtype=np.float32)
    if red >= 180:
        vector[0] = 1
    elif red >= 130:
        vector[0], vector[1] = .9, np.sqrt(1 - .9**2)
    else:
        vector[2] = 1
    return {"embedding": vector, "thumbnail": photo(red), "quality": 90}


class SyntheticEngine:
    def detect(self, jpeg):
        red = Image.open(BytesIO(jpeg)).getpixel((0, 0))[0]
        return {"faces": [face(red)] if red > 80 else [], "rejected": 0}


def offline(request):
    raise httpx.ConnectError("Synthetic camera offline")


def test_burst_reference_duplicates_wrong_person_and_atomic_enrollment(tmp_path):
    with TestClient(create_app(tmp_path, httpx.MockTransport(offline), face_engine=SyntheticEngine())) as client:
        hub = client.app.state.hub
        capture = client.portal.call(hub.faces.observe, face(), hub.settings.camera_url)
        response = client.post('/api/people/upload', data={"name": "Burst Person", "burst": "true", "capture_id": capture},
                               files=[('files', (f'{red}.jpg', photo(red), 'image/jpeg')) for red in (200, 150, 100)])
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['samples_added'] == 2 and result['burst_frames_added'] == 1
        assert [item['status'] for item in result['results']] == ['skipped', 'added', 'rejected']
        gallery = client.get('/api/faces').json()
        assert not gallery['unknowns'] and gallery['people'][0]['samples'] == 2
        assert client.portal.call(hub.faces.match, face(100)['embedding'])[0] is None
        # The reference cannot be relabelled by changing the burst form's name.
        response = client.post('/api/people/upload', data={"name": "Other Name", "burst": "true", "capture_id": capture},
                               files=[('files', ('photo.jpg', photo(150), 'image/jpeg'))])
        assert response.status_code == 400
        assert len(client.get('/api/faces').json()['people']) == 1


def test_new_burst_identity_dedup_replay_and_invalid_frame(tmp_path):
    with TestClient(create_app(tmp_path, httpx.MockTransport(offline), face_engine=SyntheticEngine())) as client:
        files = [('files', (f'{index}.jpg', photo(red), 'image/jpeg')) for index, red in enumerate((200, 200, 150, 100, 50))]
        response = client.post('/api/people/upload', data={"name": "Burst Person", "burst": "true"}, files=files)
        assert response.status_code == 200, response.text
        assert response.json()['samples_added'] == 2
        assert [r['status'] for r in response.json()['results']] == ['added', 'skipped', 'added', 'rejected', 'rejected']
        response = client.post('/api/people/upload', data={"name": "Burst Person", "burst": "true"},
                               files=[('files', ('same.jpg', photo(), 'image/jpeg'))])
        assert response.status_code == 200 and response.json()['samples_added'] == 0
        assert client.get('/api/faces').json()['people'][0]['samples'] == 2
        response = client.post('/api/people/upload', data={"name": "Empty", "burst": "true"},
                               files=[('files', ('none.jpg', photo(50), 'image/jpeg'))])
        assert response.status_code == 422
        assert len(client.get('/api/faces').json()['people']) == 1


def test_burst_frame_offline_and_reuses_active_stream(tmp_path):
    with TestClient(create_app(tmp_path, httpx.MockTransport(offline), face_engine=SyntheticEngine())) as client:
        assert client.get('/api/enrollment/frame').status_code == 503
        hub = client.app.state.hub
        async def active_frame():
            hub.online = True
            hub.frame, hub.last_frame_at = photo(), time.monotonic()
        client.portal.call(active_frame)
        response = client.get('/api/enrollment/frame')
        assert response.status_code == 200 and response.content == photo()
        assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('turns,box', [(0, [10, 20, 30, 40]), (1, [20, 60, 40, 30]),
                                    (2, [60, 140, 30, 40]), (3, [140, 10, 40, 30])])
def test_rotated_overlay_returns_original_geometry(turns, box):
    assert restore_box(box, turns, 100, 200) == [10, 20, 30, 40]


def test_real_model_sideways_and_three_scales_keep_identity():
    import cv2
    root = Path(__file__).resolve().parents[1]
    fixture = root / 'artifacts/face-model-test.jpg'
    if not fixture.exists() or not (root / 'models/face_recognition_sface_2021dec.onnx').exists():
        pytest.skip('Download the official models and public OpenCV sample first')
    image = cv2.imread(str(fixture))
    engine = OpenCVEngine(root / 'models')
    original = engine.detect(fixture.read_bytes())['faces'][0]
    for turns in range(4):
        rotated = np.ascontiguousarray(np.rot90(image, turns))
        for scale in (.8, 1, 1.2):
            sample = cv2.resize(rotated, None, fx=scale, fy=scale)
            _, jpeg = cv2.imencode('.jpg', sample)
            result = engine.detect(jpeg.tobytes())
            assert len(result['faces']) == 1, (turns, scale)
            found = result['faces'][0]
            assert float(original['embedding'] @ found['embedding']) > .5
            x,y,w,h = found['box']
            assert x >= 0 and y >= 0 and x+w <= result['image_width'] and y+h <= result['image_height']
