"""Desktop/mobile browser checks. Online checks intercept APIs with synthetic data."""
import asyncio
import base64
import json
from pathlib import Path
import sys

from playwright.async_api import async_playwright, expect

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts"


async def main():
    ARTIFACTS.mkdir(exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        response = await page.goto(BASE)
        assert response.status == 200
        await expect(page.locator("#connection-label")).not_to_have_text("Checking camera")
        await expect(page.locator("#dashboard-updates")).to_have_text("Live · SSE")
        await page.screenshot(path=str(ARTIFACTS / "dashboard-desktop.png"), full_page=True)
        await page.get_by_role("button", name="Camera settings", exact=True).click()
        await expect(page.locator("#settings-dialog")).to_be_visible()
        await page.keyboard.press("Escape")
        await page.get_by_role("button", name="Open on your phone").click()
        await expect(page.locator("#phone-dialog")).to_be_visible()
        assert "127.0.0.1" not in await page.locator("#phone-address").inner_text()
        await page.keyboard.press("Escape")
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        nav = await page.locator('nav').bounding_box()
        assert nav and abs(nav['y'] + nav['height'] - 844) < 2, nav
        await expect(page.get_by_role('link', name='People', exact=True)).to_be_visible()
        await page.screenshot(path=str(ARTIFACTS / "dashboard-mobile.png"), full_page=True)
        await page.get_by_role("button", name="Camera settings", exact=True).click()
        await page.locator("#camera-url").fill("8.8.8.8")
        await page.get_by_role("button", name="Save & connect").click()
        await expect(page.locator("#settings-error")).not_to_be_empty()
        await page.keyboard.press("Escape")
        print("Actual running dashboard: desktop/mobile layout, dialogs, validation passed.")

        # A visible TEST CAMERA label prevents confusing these frames with real camera evidence.
        jpeg = await page.evaluate("""() => {
            const c=document.createElement('canvas'); c.width=640; c.height=480;
            const x=c.getContext('2d'); x.fillStyle='#244533'; x.fillRect(0,0,640,480);
            x.fillStyle='white'; x.font='28px sans-serif'; x.fillText('SYNTHETIC TEST CAMERA',120,240);
            return c.toDataURL('image/jpeg').split(',')[1];
        }""")
        frame = base64.b64decode(jpeg)
        manual = False
        async def mock(route):
            nonlocal manual
            path = route.request.url.split("/api/")[1].split("?")[0]
            if path == "updates":
                return await route.fulfill(status=503, body="SSE disabled for synthetic API-control checks")
            if path == "stream":
                body = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n--frame--\r\n"
                return await route.fulfill(content_type="multipart/x-mixed-replace; boundary=frame", body=body)
            camera = {"device_id": "synthetic-camera", "boot_id": "test", "uptime_ms": 123000,
                      "rssi": -50, "manual_live": manual, "stream_allowed": manual,
                      "pir_enabled": True, "pir_ready": True, "motion_active": False,
                      "sound_enabled": True, "sound_input_level": 1, "sound_active_low": True}
            if path.startswith("live/"):
                manual = path.endswith("start")
                camera.update(manual_live=manual, stream_allowed=manual)
            if path in ("status", "live/start", "live/stop"):
                body = {"online": True, "camera_url": "http://192.168.1.99", "camera": camera,
                        "error": None, "video": {"receiving": manual, "error": None}}
            elif path == "events":
                body = {"events": [{"id": 1, "type": "motion", "uptime_ms": 60000, "received_at": "2026-11-10T12:00:00Z"}]}
            elif path == "snapshot":
                return await route.fulfill(content_type="image/jpeg", body=frame)
            else:
                return await route.continue_()
            await route.fulfill(content_type="application/json", body=json.dumps(body))
        await page.route("**/api/**", mock)
        await page.reload()
        await expect(page.locator("#connection-label")).to_have_text("Camera connected")
        await expect(page.get_by_text("Motion detected", exact=True)).to_be_visible()
        await page.get_by_role("button", name="Start live", exact=True).click()
        await expect(page.locator("#stream")).to_be_visible()
        assert await page.locator("#stream").evaluate("image => image.naturalWidth") == 640
        async with page.expect_download() as download:
            await page.get_by_role("button", name="Snapshot", exact=True).click()
        assert (await download.value).suggested_filename == "doorbell-snapshot.jpg"
        await page.get_by_role("button", name="Stop", exact=True).click()
        await expect(page.locator("#stream")).to_be_hidden()
        assert not errors, errors
        print("Synthetic API browser checks: live start/stop, JPEG display, event feed, snapshot passed; no JavaScript errors.")
        await browser.close()


asyncio.run(main())
