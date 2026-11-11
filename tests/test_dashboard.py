"""Protocol tests use a synthetic ESP32, never claim to exercise real hardware."""
import asyncio

from fastapi.testclient import TestClient
import httpx
import pytest
from pydantic import ValidationError

from server.app import CameraHub, Settings, create_app


def camera_status(boot="one", manual=False):
    return {"device_id": "test-camera", "boot_id": boot, "uptime_ms": 62000,
            "manual_live": manual, "stream_allowed": manual}


def event(boot="one"):
    return {"device_id": "test-camera", "boot_id": boot, "id": 1,
            "type": "motion", "uptime_ms": 61000}


@pytest.mark.asyncio
async def test_poll_deduplicates_and_survives_camera_reboot(tmp_path):
    boot, cursors = "one", []
    def handler(request):
        if request.url.path == "/api/status":
            return httpx.Response(200, json=camera_status(boot))
        cursors.append(int(request.url.params["after"]))
        return httpx.Response(200, json={"boot_id": boot, "next_after": 1, "events": [event(boot)]})
    hub = CameraHub(tmp_path, httpx.MockTransport(handler))
    try:
        await hub.poll_once()
        first_received = hub.events()[0]["received_at"]
        await hub.poll_once()
        assert len(hub.events()) == 1
        assert hub.events()[0]["received_at"] == first_received
        boot = "two"
        await hub.poll_once()
        assert len(hub.events()) == 2
        assert cursors == [0, 1, 0]
    finally:
        await hub.close()
    reopened = CameraHub(tmp_path, httpx.MockTransport(handler))
    assert len(reopened.events()) == 2
    await reopened.close()


@pytest.mark.asyncio
async def test_unreachable_camera_preserves_but_marks_last_status(tmp_path):
    available = True
    def handler(request):
        if not available:
            raise httpx.ConnectTimeout("offline")
        if request.url.path.endswith("status"):
            return httpx.Response(200, json=camera_status())
        return httpx.Response(200, json={"boot_id": "one", "next_after": 0, "events": []})
    hub = CameraHub(tmp_path, httpx.MockTransport(handler))
    try:
        await hub.poll_once()
        assert hub.online
        available = False
        await hub.poll_once()
        assert not hub.online
        assert hub.status["device_id"] == "test-camera"
        assert hub.last_seen and hub.error
    finally:
        await hub.close()


@pytest.mark.asyncio
async def test_settings_are_persistent_and_history_is_per_camera(tmp_path):
    hub = CameraHub(tmp_path)
    hub.save_event(event())
    await hub.configure(Settings(camera_url="192.168.1.5"))
    assert hub.events() == []
    await hub.close()
    hub = CameraHub(tmp_path)
    assert hub.settings.camera_url == "http://192.168.1.5"
    await hub.configure(Settings())
    assert len(hub.events()) == 1
    await hub.close()


@pytest.mark.parametrize("address", ["https://192.168.1.2", "http://8.8.8.8", "http://192.168.1.2/api", "http://user:pass@192.168.1.2", "http://0.0.0.0", "http://169.254.169.254", "http://192.168.1.2:81"])
def test_camera_setting_rejects_invalid_targets(address):
    with pytest.raises(ValidationError):
        Settings(camera_url=address)


@pytest.mark.asyncio
async def test_two_viewers_share_one_upstream_and_release_it(tmp_path):
    opened, closed = 0, 0
    class SyntheticStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                # Split JPEG markers across transport chunks.
                for chunk in (b"boundary\r\n\xff", b"\xd8synthetic", b"frame\xff", b"\xd9\r\n"):
                    yield chunk
                    await asyncio.sleep(.005)
        async def aclose(self):
            nonlocal closed
            closed += 1
    def handler(request):
        nonlocal opened
        opened += 1
        assert str(request.url).endswith(":81/api/stream")
        return httpx.Response(200, headers={"content-type": "multipart/x-mixed-replace; boundary=boundary"}, stream=SyntheticStream())
    hub = CameraHub(tmp_path, httpx.MockTransport(handler))
    first, second = hub.frames(), hub.frames()
    try:
        frames = await asyncio.wait_for(asyncio.gather(anext(first), anext(second)), 2)
        assert opened == 1 and hub.viewers == 2
        assert frames[0] == frames[1]
        assert b"\xff\xd8syntheticframe\xff\xd9" in frames[0]
        await first.aclose()
        assert hub.viewers == 1 and closed == 0
        assert await asyncio.wait_for(anext(second), 1)
        await second.aclose()
        assert hub.viewers == 0 and hub.producer is None and closed == 1
    finally:
        await first.aclose()
        await second.aclose()
        await hub.close()


def test_dashboard_api_controls_snapshot_and_origin_check(tmp_path):
    actions = []
    def handler(request):
        if request.url.path.startswith("/api/live/"):
            actions.append(request.url.path)
            return httpx.Response(200, json=camera_status(manual=request.url.path.endswith("start")))
        if request.url.path == "/api/snapshot":
            return httpx.Response(200, content=b"\xff\xd8test\xff\xd9")
        if request.url.path == "/api/events":
            return httpx.Response(200, json={"boot_id": "one", "next_after": 1, "events": [event()]})
        return httpx.Response(200, json=camera_status())
    with TestClient(create_app(tmp_path, httpx.MockTransport(handler), poll_interval=60)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.post("/api/live/start").json()["camera"]["manual_live"]
        assert not client.post("/api/live/stop").json()["camera"]["manual_live"]
        assert actions == ["/api/live/start", "/api/live/stop"]
        assert client.get("/api/snapshot").headers["content-type"] == "image/jpeg"
        assert client.post("/api/events", json=event()).status_code == 200
        assert client.post("/api/events", json=event()).status_code == 200
        assert len(client.get("/api/events").json()["events"]) == 1
        assert client.post("/api/events", json={**event(), "device_id": "other"}).status_code == 409
        assert client.put("/api/settings", json={"camera_url": "http://8.8.8.8"}).status_code == 422
        assert client.post("/api/live/start", headers={"Origin": "http://another-site.test"}).status_code == 403


def test_failed_control_returns_actionable_502(tmp_path):
    def offline(request):
        raise httpx.ConnectError("offline")
    with TestClient(create_app(tmp_path, httpx.MockTransport(offline))) as client:
        response = client.post("/api/live/start")
        assert response.status_code == 502
        assert "Wi-Fi" in response.json()["detail"]
