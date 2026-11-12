"""Synthetic lighting checks; physical camera exposure is verified after upload."""
import asyncio
from io import BytesIO
import time

import httpx
from PIL import Image
import pytest

from server.app import CameraHub, Settings
from server.lighting import AutomaticLight, frame_brightness


def photo(level):
    buffer = BytesIO()
    Image.new("RGB", (320, 240), (level,) * 3).save(buffer, "JPEG")
    return buffer.getvalue()


def test_brightness_and_hysteresis_hold():
    assert frame_brightness(photo(20)) == pytest.approx(20, abs=1)
    light = AutomaticLight()
    for at in (0, 3):
        light.update(20, at)
        assert not light.enabled
    light.update(20, 6)
    assert light.enabled
    # Bright samples cannot toggle immediately after turning on.
    for at in (9, 12, 15):
        light.update(180, at)
        assert light.enabled
    light.update(180, 36)
    assert not light.enabled
    # Noise in the threshold gap and isolated dark samples keep the lamp off.
    for at, level in ((39, 40), (42, 80), (45, 40), (48, 80)):
        light.update(level, at)
        assert not light.enabled


@pytest.mark.asyncio
async def test_native_lighting_ack_and_night_preference_survive_restart(tmp_path):
    requests = []
    def camera(request):
        assert request.headers["connection"] == "close"
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json={"night_mode": True, "camera_light_percent": 50})
    hub = CameraHub(tmp_path, httpx.MockTransport(camera))
    try:
        hub.status = {"boot_id": "test", "night_mode": False}
        await hub.configure(Settings(night_mode=True, face_monitoring=False))
        await hub.apply_camera_controls(hub.settings.camera_url, True, True)
        assert requests == [("POST", "/api/night/start"), ("POST", "/api/light/start")]
    finally:
        await hub.close()
    reopened = CameraHub(tmp_path)
    assert reopened.settings.night_mode and not reopened.settings.face_monitoring
    await reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{"night_mode": False}, {"night_mode": True, "camera_light_percent": 0}, []])
async def test_native_control_rejects_unconfirmed_changes(tmp_path, response):
    hub = CameraHub(tmp_path, httpx.MockTransport(lambda _: httpx.Response(200, json=response)))
    hub.status = {"night_mode": False}
    try:
        with pytest.raises(ValueError):
            await hub.apply_camera_controls(hub.settings.camera_url, True, True)
    finally:
        await hub.close()


@pytest.mark.asyncio
async def test_older_firmware_controls_keep_light_off_when_bright(tmp_path):
    requests = []
    def camera(request):
        requests.append(dict(request.url.params))
        return httpx.Response(200)
    hub = CameraHub(tmp_path, httpx.MockTransport(camera))
    try:
        await hub.apply_camera_controls(hub.settings.camera_url, False, False)
        assert requests == [{"var": "led_intensity", "val": "0"}, {"var": "aec", "val": "1"},
                            {"var": "agc", "val": "1"}, {"var": "brightness", "val": "0"},
                            {"var": "ae_level", "val": "0"}]
    finally:
        await hub.close()


@pytest.mark.asyncio
async def test_face_and_lighting_workers_share_idle_snapshot(tmp_path):
    requests = []
    async def camera(request):
        requests.append(request.url.path)
        await asyncio.sleep(.01)
        return httpx.Response(200, content=photo(20))
    hub = CameraHub(tmp_path, httpx.MockTransport(camera))
    try:
        frames = await asyncio.gather(hub.camera_snapshot(hub.settings.camera_url), hub.camera_snapshot(hub.settings.camera_url))
        assert frames[0] == frames[1]
        assert requests == ["/api/snapshot"]
    finally:
        await hub.close()


@pytest.mark.asyncio
async def test_controls_worker_uses_live_frame_and_only_applies_changes(tmp_path):
    requests = []
    def camera(request):
        requests.append(request.url.path)
        assert request.url.path != "/api/snapshot"
        return httpx.Response(200, json={"night_mode": False, "camera_light_percent": 0})
    hub = CameraHub(tmp_path, httpx.MockTransport(camera))
    hub.status = {"boot_id": "one", "night_mode": False}
    hub.online = True
    hub.frame, hub.last_frame_at = photo(180), time.monotonic()
    hub.camera_controls_task = asyncio.create_task(hub.maintain_camera_controls())
    try:
        async with asyncio.timeout(2):
            while not hub.state()["camera_controls"]["applied"]:
                await asyncio.sleep(.01)
        await asyncio.sleep(1.1)
        assert requests == ["/api/night/stop", "/api/light/stop"]
        assert hub.light.brightness > 115 and not hub.light.enabled
    finally:
        await hub.close()
