"""Isolated real-model enrollment QA with an OpenCV sample, never the user's gallery."""
import asyncio
import json
from pathlib import Path
import socket
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from playwright.async_api import async_playwright, expect
import uvicorn

from server.app import create_app


async def main():
    fixture = ROOT / "artifacts/face-model-test.jpg"
    if not fixture.is_file():
        raise RuntimeError("Place the OpenCV samples/data/lena.jpg test fixture in artifacts/face-model-test.jpg first.")
    jpeg = fixture.read_bytes()
    started = time.monotonic()
    motion = {"active": True, "event": 1, "triggers": 1}
    class SampleStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
                await asyncio.sleep(.2)
    def camera(request):
        if request.url.path == "/api/stream":
            return httpx.Response(200, stream=SampleStream(), headers={"content-type": "multipart/x-mixed-replace; boundary=frame"})
        if request.url.path == "/api/snapshot":
            return httpx.Response(200, content=jpeg, headers={"content-type": "image/jpeg"})
        if request.url.path == "/api/events":
            return httpx.Response(200, json={"boot_id": "qa-only", "next_after": 0, "events": []})
        uptime = 65000 + int((time.monotonic() - started) * 1000)
        return httpx.Response(200, json={"device_id": "isolated-qa-camera", "boot_id": "qa-only", "uptime_ms": uptime,
            "manual_live": False, "stream_allowed": True, "rssi": -50, "pir_enabled": True,
            "pir_ready": True, "motion_active": False, "sound_enabled": True,
            "sound_input_level": 1, "sound_active_low": True, "sensor_timing_version": 1,
            "sensor_levels": {"motion": {"level": int(motion["active"]), "active_level": 1,
                "level_for_ms": uptime - 65000, "trigger_count": motion["triggers"],
                "active_event_id": motion["event"], "last_high_ms": 4800, "last_low_ms": 50},
                "sound": {"level": 1, "active_level": 0, "level_for_ms": uptime - 65000,
                          "trigger_count": 0, "active_event_id": 0, "last_low_ms": 8}}})
    with tempfile.TemporaryDirectory(prefix="doorbell-face-qa-") as data:
        import cv2
        frame = cv2.imread(str(fixture))
        frame = cv2.resize(frame, (512, 512))
        yuv = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420).tobytes()
        fake_video = Path(data) / "sample-camera.y4m"
        fake_video.write_bytes(b"YUV4MPEG2 W512 H512 F10:1 Ip A1:1 C420\n" + (b"FRAME\n" + yuv) * 10)
        app = create_app(data, httpx.MockTransport(camera))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    await asyncio.sleep(.02)
            base = f"http://127.0.0.1:{port}"
            async with async_playwright() as p:
                browser = await p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True, args=[
                    "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                    f"--use-file-for-fake-video-capture={fake_video}",
                    "--host-resolver-rules=MAP insecure-camera.test 127.0.0.1", "--no-proxy-server"])
                try:
                    page = await browser.new_page(viewport={"width": 1440, "height": 1100})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(base)
                    await expect(page.locator('#dashboard-updates')).to_have_text('Live · SSE')
                    async with httpx.AsyncClient() as client:
                        await client.post(base + '/api/events', json={"device_id":"isolated-qa-camera", "boot_id":"qa-only", "id":1, "type":"motion", "uptime_ms":65000, "phase":"triggered"})
                    await expect(page.get_by_text('Motion detected', exact=True)).to_be_visible()
                    await expect(page.locator(".unknown-card")).to_have_count(1, timeout=15000)
                    await expect(page.locator('#face-overlay .unknown-face')).to_have_count(1, timeout=15000)
                    await expect(page.locator('#face-overlay')).to_be_visible()
                    await expect(page.locator('#motion-timing')).to_contain_text('HIGH · active')
                    await expect(page.locator('.event-duration')).to_contain_text('ongoing')
                    await page.evaluate("""() => { const banner=document.createElement('p'); banner.textContent='ISOLATED QA · OpenCV sample image · no real ESP32 or household profile'; banner.style='background:#f7ecd4;padding:14px;text-align:center;margin:0'; document.body.prepend(banner); }""")
                    assert await page.locator('.unknown-card img').evaluate('img => img.complete && img.naturalWidth > 0')
                    await page.locator('.enroll-form input').fill('QA Sample')
                    # A background refresh must not erase an in-progress name or lose focus.
                    await page.wait_for_timeout(3500)
                    await expect(page.locator('.enroll-form input')).to_have_value('QA Sample')
                    await page.screenshot(path=str(ROOT / 'artifacts/face-enrollment-desktop.png'), full_page=True)
                    await page.get_by_role('button', name='Save person', exact=True).click()
                    await expect(page.locator('.person-card h4')).to_have_text('QA Sample')
                    await expect(page.locator('.unknown-card')).to_have_count(0)
                    await expect(page.locator('#face-overlay .matched-face text')).to_have_text('QA Sample', timeout=15000)
                    async def check_alignment():
                        error = await page.evaluate('''() => {
                            const v=document.querySelector('#viewport').getBoundingClientRect();
                            const svg=document.querySelector('#face-overlay'), box=svg.querySelector('.face-box');
                            const b=box.getBBox(), actual=box.getBoundingClientRect(), view=svg.viewBox.baseVal;
                            const scale=Math.min(v.width/view.width,v.height/view.height);
                            return Math.max(Math.abs(actual.left-(v.left+(v.width-view.width*scale)/2+b.x*scale)),
                              Math.abs(actual.top-(v.top+(v.height-view.height*scale)/2+b.y*scale)),
                              Math.abs(actual.width-b.width*scale),Math.abs(actual.height-b.height*scale));
                        }''')
                        assert error < 3, error
                    await check_alignment()
                    async with httpx.AsyncClient() as client:
                        async with asyncio.timeout(10):
                            while True:
                                result = (await client.get(base + '/api/faces')).json()
                                if result['recent'] and result['recent'][0]['similarity'] is not None:
                                    assert result['recent'][0]['similarity'] > .99
                                    break
                                await asyncio.sleep(.1)
                    await page.set_viewport_size({"width": 390, "height": 844})
                    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    await check_alignment()
                    async with httpx.AsyncClient() as client:
                        motion.update(active=False, event=0)
                        await client.post(base + '/api/events', json={"device_id":"isolated-qa-camera", "boot_id":"qa-only", "id":2, "type":"motion", "uptime_ms":69800, "phase":"cleared"})
                    await expect(page.locator('.event-duration')).to_contain_text('4 sec · cleared')
                    assert await page.locator('#activity-list .event').count() == 1
                    async with httpx.AsyncClient() as client:
                        motion.update(active=True, event=3, triggers=2)
                        await client.post(base + '/api/events', json={"device_id":"isolated-qa-camera", "boot_id":"qa-only", "id":3, "type":"motion", "uptime_ms":69900, "phase":"triggered"})
                    await expect(page.locator('#activity-list .event')).to_have_count(2)
                    await expect(page.locator('#motion-timing')).to_contain_text('2 alerts this boot')
                    await page.screenshot(path=str(ROOT / 'artifacts/face-enrollment-mobile.png'), full_page=True)
                    await page.locator('#face-monitoring').uncheck()
                    await expect(page.locator('#face-status')).to_contain_text('paused')
                    await expect(page.locator('#face-overlay')).to_be_hidden()
                    page.once('dialog', lambda dialog: dialog.accept())
                    await page.get_by_role('button', name='Remove QA Sample', exact=True).click()
                    await expect(page.locator('.person-card')).to_have_count(0)
                    # Test a face without saving an unknown capture or profile.
                    await page.locator('#test-file').set_input_files(str(fixture))
                    await page.get_by_role('button', name='Run test', exact=True).click()
                    await expect(page.locator('.test-result h4')).to_have_text('Unknown')
                    await expect(page.locator('.person-card')).to_have_count(0)
                    await expect(page.locator('.unknown-card')).to_have_count(0)
                    # Upload real photos into the isolated gallery, then test matching.
                    await page.get_by_role('button', name='Upload photos', exact=True).click()
                    await page.locator('#upload-name').fill('Uploaded QA')
                    await page.locator('#upload-files').set_input_files([str(fixture), str(fixture)])
                    await page.get_by_role('button', name='Add photos to recognition', exact=True).click()
                    await expect(page.locator('#upload-message')).to_have_text('2 photos added for Uploaded QA.')
                    await page.get_by_role('button', name='Close photo upload', exact=True).click()
                    await expect(page.locator('.person-card h4')).to_have_text('Uploaded QA')
                    await expect(page.locator('.person-description p')).to_have_text('2 saved samples')
                    await page.get_by_role('button', name='Run test', exact=True).click()
                    await expect(page.locator('.test-result h4')).to_have_text('Uploaded QA')
                    # Device webcam path uses a labelled synthetic camera, never physical hardware.
                    await page.get_by_role('button', name='Use device camera', exact=True).click()
                    await expect(page.locator('#device-camera-video')).to_be_visible()
                    await expect(page.locator('#test-message')).to_contain_text('Live check', timeout=15000)
                    await expect(page.locator('.test-result h4')).to_have_text('Uploaded QA')
                    await page.evaluate('window.qaTracks = document.getElementById("device-camera-video").srcObject.getTracks()')
                    await page.get_by_role('button', name='Stop camera', exact=True).click()
                    await expect(page.locator('#device-camera-video')).to_be_hidden()
                    assert await page.evaluate('window.qaTracks.every(track => track.readyState === "ended")')
                    await page.screenshot(path=str(ROOT / 'artifacts/face-upload-test-mobile.png'), full_page=True)
                    # Existing-person shortcut and precise rejection message.
                    await page.get_by_role('button', name='Add photos for Uploaded QA', exact=True).click()
                    await expect(page.locator('#upload-name')).to_have_value('Uploaded QA')
                    await page.locator('#upload-files').set_input_files({"name": "broken.jpg", "mimeType": "image/jpeg", "buffer": b"not an image"})
                    await page.get_by_role('button', name='Add photos to recognition', exact=True).click()
                    await expect(page.locator('#upload-message')).to_contain_text('could not be read')
                    await expect(page.locator('#upload-results')).to_be_visible()
                    await page.get_by_role('button', name='Close photo upload', exact=True).click()
                    # An insecure LAN-style HTTP origin must show the HTTPS requirement.
                    insecure = await browser.new_page(viewport={"width":390,"height":844})
                    await insecure.goto(f'http://insecure-camera.test:{port}/#faces')
                    await expect(insecure.locator('#device-camera-requirement')).to_be_visible()
                    await insecure.get_by_role('button', name='Use device camera', exact=True).click()
                    await expect(insecure.locator('#test-message')).to_contain_text('requires HTTPS')
                    assert await insecure.locator('#test-camera-photo').get_attribute('capture') == 'user'
                    await insecure.close()
                    assert not errors, errors
                    print(json.dumps({"isolated_real_model_test": "passed", "unknown_thumbnail": True,
                        "name_preserved_during_refresh": True, "enrollment": True,
                        "subsequent_sample_recognized": True, "mobile_layout": True,
                        "pause_and_delete": True, "real_model_photo_upload": True, "sse_event_updates": True,
                        "read_only_recognition_test": True, "fake_device_camera_matching_and_stop": True,
                        "insecure_origin_help": True, "precise_upload_errors": True,
                        "live_unknown_and_name_overlays": True, "desktop_mobile_overlay_alignment": True,
                        "sensor_durations_and_retrigger_count": True, "overlay_clears_when_paused": True,
                        "javascript_errors": errors}))
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            await task


asyncio.run(main())
