"""Storage/matching and camera integration tests use explicitly synthetic vectors."""
import asyncio
from io import BytesIO
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from server.app import CameraHub, Settings, create_app
from server.faces import FaceStore, OpenCVEngine, prepare_photo


CAMERA = "http://192.168.12.140"


def detection(index=0):
    vector = np.zeros(128, dtype=np.float32)
    vector[index] = 1
    return {"embedding": vector, "thumbnail": b"\xff\xd8synthetic\xff\xd9", "quality": 90}


class NoFaces:
    def detect(self, jpeg):
        return {"faces": [], "rejected": 0}


def test_name_unknown_then_recognize_without_retraining_and_restart(tmp_path):
    store = FaceStore(tmp_path)
    first = store.observe(detection(), CAMERA)
    assert len(store.gallery(CAMERA)["unknowns"]) == 1
    store.observe(detection(), CAMERA)
    assert len(store.gallery(CAMERA)["unknowns"]) == 1
    person = store.enroll(first, "  Test   Person  ")
    assert person["name"] == "Test Person"
    assert store.enroll(first, "test person")["id"] == person["id"]  # idempotent
    store.close()
    store = FaceStore(tmp_path)
    assert store.match(detection()["embedding"])[0] == person["id"]
    store.observe(detection(), CAMERA)
    data = store.gallery(CAMERA)
    assert len(data["unknowns"]) == 0
    assert data["recent"][0]["name"] == "Test Person"
    assert data["people"][0]["samples"] == 1
    # An unrelated embedding stays unknown rather than forcing the nearest name.
    assert store.match(detection(1)["embedding"])[0] is None
    other = store.observe(detection(1), CAMERA)
    store.enroll(other, "TEST PERSON")
    assert store.gallery(CAMERA)["people"][0]["samples"] == 2
    assert len(store.gallery(CAMERA)["people"]) == 1
    store.delete_person(person["id"])
    assert store.gallery(CAMERA)["people"] == []
    assert store.gallery(CAMERA)["recent"] == []
    assert store.match(detection()["embedding"])[0] is None
    with pytest.raises(KeyError):
        store.image(first)
    store.close()


def test_ambiguous_matches_are_unknown(tmp_path):
    store = FaceStore(tmp_path)
    first = store.observe(detection(), CAMERA)
    store.enroll(first, "Person A")
    other = store.observe(detection(1), CAMERA)
    store.enroll(other, "Person B")
    query = detection()["embedding"] + detection(1)["embedding"]
    assert store.match(query)[0] is None
    store.close()


def test_captures_are_scoped_to_camera_and_names_are_validated(tmp_path):
    store = FaceStore(tmp_path)
    capture = store.observe(detection(), CAMERA)
    assert not store.gallery("http://192.168.1.4")["unknowns"]
    for invalid in ("   ", "x" * 81, "Test\x00Person"):
        with pytest.raises(ValueError):
            store.enroll(capture, invalid)
    with pytest.raises(KeyError):
        store.enroll("missing", "Test")
    store.close()


def test_enrollment_api_and_profile_deletion(tmp_path):
    def offline(request):
        raise httpx.ConnectError("Camera offline")
    with TestClient(create_app(tmp_path, httpx.MockTransport(offline), face_engine=NoFaces())) as client:
        # Use the test portal so all SQLite operations stay on the app's event loop.
        hub = client.app.state.hub
        capture = client.portal.call(hub.faces.observe, detection(), CAMERA)
        assert len(client.get("/api/faces").json()["unknowns"]) == 1
        assert client.get(f"/api/faces/{capture}/image").headers["content-type"] == "image/jpeg"
        assert client.post(f"/api/faces/{capture}/enroll", json={"name": "  "}).status_code == 400
        response = client.post(f"/api/faces/{capture}/enroll", json={"name": "Test Person"})
        assert response.status_code == 200
        person = response.json()
        assert client.get(f"/api/people/{person['id']}/image").status_code == 200
        assert not client.get("/api/faces").json()["unknowns"]
        assert client.post(f"/api/faces/{capture}/enroll", json={"name": "Wrong person"}).status_code == 400
        assert client.delete(f"/api/people/{person['id']}").status_code == 200
        assert client.get(f"/api/people/{person['id']}/image").status_code == 404
        assert client.post("/api/faces/missing/enroll", json={"name": "Test"}).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("live_stream", [False, True])
async def test_background_detection_uses_existing_stream_or_idle_snapshot(tmp_path, live_stream):
    requests = []
    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, content=b"jpeg-test")
    class TestEngine:
        def detect(self, jpeg):
            assert jpeg == b"jpeg-test"
            return {"faces": [detection()], "rejected": 0}
    hub = CameraHub(tmp_path, httpx.MockTransport(handler), face_engine=TestEngine())
    hub.online = True
    if live_stream:
        hub.frame, hub.frame_id, hub.last_frame_at = b"jpeg-test", 1, time.monotonic()
        hub.producer = asyncio.create_task(asyncio.sleep(60))
    hub.face_task = asyncio.create_task(hub.recognize_faces())
    try:
        async def observed():
            while not hub.face_last_checked:
                await asyncio.sleep(.01)
        await asyncio.wait_for(observed(), 2)
        assert len(hub.face_status()["unknowns"]) == 1
        assert requests == ([] if live_stream else ["/api/snapshot"])
    finally:
        await hub.close()


@pytest.mark.asyncio
async def test_paused_monitor_does_not_capture_images(tmp_path):
    def forbidden(request):
        pytest.fail("Paused monitoring requested an image")
    hub = CameraHub(tmp_path, httpx.MockTransport(forbidden), face_engine=NoFaces())
    hub.online = True
    await hub.configure(Settings(face_monitoring=False))
    hub.face_task = asyncio.create_task(hub.recognize_faces())
    await asyncio.sleep(.03)
    assert hub.online  # Toggle must not interrupt the camera/video.
    assert hub.face_last_checked is None
    await hub.close()


def photo(color="red", format="PNG"):
    from PIL import Image
    buffer = BytesIO()
    Image.new("RGB", (192, 192), color).save(buffer, format=format)
    return buffer.getvalue()


class UploadTestEngine:
    def detect(self, jpeg):
        from PIL import Image
        red, green, blue = Image.open(BytesIO(jpeg)).getpixel((0, 0))
        if blue > 150:
            return {"faces": [detection(), detection(1)], "rejected": 0}
        if green > 80:
            return {"faces": [], "rejected": 1}
        return {"faces": [detection()] if red > 150 else [], "rejected": 0}


def test_photo_upload_enrolls_offline_and_extends_existing_profile(tmp_path):
    def offline(request):
        raise httpx.ConnectError("Camera offline")
    with TestClient(create_app(tmp_path, httpx.MockTransport(offline), face_engine=UploadTestEngine())) as client:
        response = client.post("/api/people/upload", data={"name": "Upload Person"}, files=[
            ("files", ("first.png", photo(), "image/png")),
            ("files", ("second.png", photo(), "image/png"))])
        assert response.status_code == 200, response.text
        assert response.json()["samples_added"] == 2
        data = client.get("/api/faces").json()
        assert data["people"][0]["name"] == "Upload Person"
        assert data["people"][0]["samples"] == 2
        assert data["recent"] == [] and data["unknowns"] == []  # Not a camera sighting.
        response = client.post("/api/people/upload", data={"name": "upload person"}, files={"files": ("more.png", photo(), "image/png")})
        assert response.status_code == 200
        data = client.get("/api/faces").json()
        assert len(data["people"]) == 1 and data["people"][0]["samples"] == 3
        matched, _ = client.portal.call(client.app.state.hub.faces.match, detection()["embedding"])
        assert matched == data["people"][0]["id"]


def test_upload_returns_per_photo_results_and_keeps_valid_samples(tmp_path):
    def offline(request):
        raise httpx.ConnectError("offline")
    with TestClient(create_app(tmp_path, httpx.MockTransport(offline), face_engine=UploadTestEngine())) as client:
        response = client.post("/api/people/upload", data={"name": "Partial"}, files=[
            ("files", ("valid.png", photo(), "image/png")),
            ("files", ("two-people.png", photo("blue"), "image/png")),
            ("files", ("empty.png", photo("black"), "image/png")),
            ("files", ("blurry.png", photo("green"), "image/png")),
            ("files", ("broken.jpg", b"not an image", "image/jpeg"))])
        assert response.status_code == 200
        data = response.json()
        assert data["samples_added"] == 1
        assert [item["status"] for item in data["results"]] == ["added"] + ["rejected"] * 4
        assert "More than one" in data["results"][1]["reason"]
        assert client.get("/api/faces").json()["people"][0]["samples"] == 1


@pytest.mark.parametrize("contents", [b"broken file", photo("black"), photo("blue"), photo("green"), photo(format="GIF")])
def test_invalid_upload_does_not_create_profile(tmp_path, contents):
    def offline(request):
        raise httpx.ConnectError("offline")
    with TestClient(create_app(tmp_path, httpx.MockTransport(offline), face_engine=UploadTestEngine())) as client:
        response = client.post("/api/people/upload", data={"name": "Invalid"}, files={"files": ("photo.jpg", contents, "image/jpeg")})
        assert response.status_code == 422
        assert response.json()["results"][0]["status"] == "rejected"
        assert client.get("/api/faces").json()["people"] == []


def test_upload_limits_and_invalid_name(tmp_path):
    def offline(request):
        raise httpx.ConnectError("offline")
    with TestClient(create_app(tmp_path, httpx.MockTransport(offline), face_engine=UploadTestEngine())) as client:
        assert client.post("/api/people/upload", data={"name": " "}, files={"files": ("a.png", photo(), "image/png")}).status_code == 400
        too_many = [("files", (f"{n}.png", photo(), "image/png")) for n in range(11)]
        assert client.post("/api/people/upload", data={"name": "Too many"}, files=too_many).status_code == 413
        response = client.post("/api/people/upload", data={"name": "Too big"}, files={"files": ("large.jpg", b"x" * (8 * 1024 * 1024 + 1), "image/jpeg")})
        assert response.status_code == 422
        assert "8 MB" in response.json()["results"][0]["reason"]
        assert client.get("/api/faces").json()["people"] == []


def test_uploaded_phone_photo_orientation_and_metadata():
    from PIL import Image
    buffer = BytesIO()
    image = Image.new("RGB", (100, 200), "red")
    exif = Image.Exif()
    exif[274] = 6
    image.save(buffer, format="JPEG", exif=exif)
    processed = Image.open(BytesIO(prepare_photo(buffer.getvalue())))
    assert processed.size == (200, 100)
    assert not processed.getexif()


def test_uploaded_gallery_persists_and_retains_twenty_samples(tmp_path):
    store = FaceStore(tmp_path)
    person = store.enroll_photos("Uploaded", [detection()] * 10)
    assert store.enroll_photos("uploaded", [detection()] * 15)["id"] == person["id"]
    store.close()
    store = FaceStore(tmp_path)
    assert store.gallery(CAMERA)["people"][0]["samples"] == 20
    assert not store.gallery(CAMERA)["recent"]
    store.close()


def test_recognition_test_is_read_only_for_known_unknown_and_multiple_faces(tmp_path):
    def offline(request):
        raise httpx.ConnectError("offline")
    with TestClient(create_app(tmp_path, httpx.MockTransport(offline), face_engine=UploadTestEngine())) as client:
        before = client.get("/api/faces").json()
        unknown = client.post("/api/faces/test", files={"file": ("test.png", photo(), "image/png")})
        assert unknown.status_code == 200
        assert unknown.json()["faces"][0]["name"] is None
        assert not unknown.json()["saved"]
        assert client.get("/api/faces").json() == before
        client.post("/api/people/upload", data={"name": "Known"}, files={"files": ("sample.png", photo(), "image/png")})
        before = client.get("/api/faces").json()
        known = client.post("/api/faces/test", files={"file": ("test.png", photo(), "image/png")}).json()
        assert known["faces"][0]["name"] == "Known"
        assert known["faces"][0]["matched"]
        assert known["faces"][0]["image_data_url"].startswith("data:image/jpeg;base64,")
        many = client.post("/api/faces/test", files={"file": ("test.png", photo("blue"), "image/png")}).json()
        assert len(many["faces"]) == 2
        assert [face["matched"] for face in many["faces"]] == [True, False]
        empty = client.post("/api/faces/test", files={"file": ("test.png", photo("black"), "image/png")}).json()
        assert not empty["faces"]
        assert client.get("/api/faces").json() == before


def test_recognition_test_rejects_bad_files_and_model_not_ready(tmp_path):
    def offline(request):
        raise httpx.ConnectError("offline")
    with TestClient(create_app(tmp_path, httpx.MockTransport(offline), face_engine=UploadTestEngine())) as client:
        assert client.post("/api/faces/test", files={"file": ("bad.jpg", b"broken", "image/jpeg")}).status_code == 422
        assert client.post("/api/faces/test", files={"file": ("big.jpg", b"x" * (8 * 1024 * 1024 + 1), "image/jpeg")}).status_code == 422
        client.portal.call(setattr, client.app.state.hub, "face_state", "error")
        assert client.post("/api/faces/test", files={"file": ("ok.png", photo(), "image/png")}).status_code == 503
        assert client.get("/api/faces").json()["people"] == []


@pytest.mark.parametrize("format", ["HEIF", "AVIF"])
def test_phone_photo_formats_are_decoded(format):
    from PIL import Image
    processed = Image.open(BytesIO(prepare_photo(photo(format=format))))
    assert processed.format == "JPEG" and processed.size == (192, 192)


def test_jpeg_with_mpo_secondary_image_uses_primary_face_photo():
    from PIL import Image
    buffer = BytesIO()
    Image.new("RGB", (192, 192), "red").save(buffer, format="MPO", save_all=True,
        append_images=[Image.new("RGB", (192, 192), "blue")])
    assert Image.open(BytesIO(buffer.getvalue())).format == "MPO"
    processed = Image.open(BytesIO(prepare_photo(buffer.getvalue())))
    assert processed.format == "JPEG"
    assert processed.getpixel((0, 0))[0] > 150


def test_real_detector_accepts_clear_closeups_at_different_resolutions():
    root = Path(__file__).resolve().parents[1]
    fixture = root / "artifacts/face-model-test.jpg"
    if not fixture.is_file() or not (root / "models/face_recognition_sface_2021dec.onnx").is_file():
        pytest.skip("Download official face models and the OpenCV sample fixture for real-model checks")
    import cv2
    engine = OpenCVEngine(root / "models")
    image = cv2.imread(str(fixture))
    detected = engine.detect(fixture.read_bytes())
    assert len(detected["faces"]) == 1
    x, y, w, h = detected["faces"][0]["box"]
    crop = image[y:y+h, x:x+w]
    for width in (160, 320, 640, 960):
        resized = cv2.resize(crop, (width, round(crop.shape[0] * width / crop.shape[1])))
        ok, jpeg = cv2.imencode(".jpg", resized)
        assert ok
        result = engine.detect(prepare_photo(jpeg.tobytes()))
        assert len(result["faces"]) == 1, f"Clear close-up rejected at width {width}"
    blurred = cv2.GaussianBlur(crop, (0, 0), 12)
    _, jpeg = cv2.imencode(".jpg", blurred)
    assert not engine.detect(jpeg.tobytes())["faces"]
