"""Isolated UI + real loopback UDP QA, using synthetic CSI and accelerated time."""
import asyncio
import json
from pathlib import Path
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
import numpy as np
from playwright.async_api import async_playwright, expect
import uvicorn
from server.app import create_app
from server.csi import HEADER, MAGIC


class Clock:
    value = 100.
    def __call__(self): return self.value


class NoFaces:
    def detect(self, jpeg): return {'faces': [], 'rejected': 0}


async def main():
    device = {'supported': True, 'enabled': False, 'sequence_start': 0}
    moving = False
    dropping = False
    done = False
    clock = Clock()
    def camera(request):
        if request.url.path == '/api/csi/config':
            device['enabled'] = json.loads(request.content)['enabled']
            device['sequence_start'] = 0
        if request.url.path == '/api/events':
            return httpx.Response(200, json={'boot_id':'synthetic-csi', 'next_after':0, 'events':[]})
        if request.url.path == '/control': return httpx.Response(200)
        return httpx.Response(200, json={'device_id':'synthetic-csi', 'boot_id':'synthetic-csi',
            'manual_live':False, 'stream_allowed':False, 'csi_pilot': dict(device)})
    with tempfile.TemporaryDirectory(prefix='doorbell-csi-qa-') as directory:
        Path(directory, 'settings.json').write_text(json.dumps({'camera_url':'http://127.0.0.1', 'face_monitoring':False}))
        app = create_app(directory, httpx.MockTransport(camera), face_engine=NoFaces(), csi_port=0)
        listener = socket.socket(); listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning'))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        producer = None
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            async with asyncio.timeout(10):
                while not server.started: await asyncio.sleep(.02)
            pilot = app.state.hub.csi; pilot.clock = clock
            # This isolated UI check skips the real exit delay; its timing is
            # covered with a controlled clock in tests/test_security.py.
            app.state.hub.security.exit_seconds = 0
            async def send():
                sequence = 0
                while not done:
                    if device['enabled']:
                        clock.value += .05
                        if not dropping:
                            iq = np.zeros((64, 2), dtype=np.int8); iq[:, 0] = 20
                            if moving and sequence % 2:
                                iq[:32, 0], iq[32:, 0] = 8, 50
                            data = HEADER.pack(MAGIC,1,1,64,2412,sequence,-50,-96,0,0) + iq.tobytes()
                            udp.sendto(data, ('127.0.0.1', pilot.port)); sequence += 1
                    await asyncio.sleep(.005)
            producer = asyncio.create_task(send())
            async with async_playwright() as p:
                browser = await p.chromium.launch(executable_path='/usr/bin/google-chrome', headless=True)
                page = await browser.new_page(viewport={'width':1440,'height':1000})
                errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
                base = f'http://127.0.0.1:{port}'
                await page.goto(base + '/#sensing')
                await expect(page.locator('#csi-start')).to_be_enabled(timeout=10000)
                assert await page.locator('#recognition-tools').count() == 0
                await page.locator('#csi-start').click()
                await expect(page.locator('#csi-state')).to_have_text('Uncalibrated', timeout=10000)
                await page.locator('#csi-calibrate').click()
                await expect(page.locator('#csi-state')).to_have_text('Quiet signal', timeout=20000)
                await page.locator('.pilot-details > summary').click()
                await page.locator('#csi-empty').click()
                await expect(page.locator('#csi-recordings a')).to_have_count(1, timeout=15000)
                await page.locator('#security-arm').click()
                await expect(page.locator('#security-state')).to_have_text('Armed away', timeout=10000)
                moving = True
                await expect(page.locator('#csi-state')).to_have_text('Elevated signal', timeout=10000)
                await page.locator('#csi-walking').click()
                await expect(page.locator('#csi-recordings a')).to_have_count(2, timeout=15000)
                await expect(page.locator('#security-incidents strong')).to_have_count(1, timeout=15000)
                await page.get_by_role('button', name='Acknowledge', exact=True).click()
                await expect(page.locator('#security-incidents')).to_contain_text('Acknowledged', timeout=10000)
                assert pilot.runs[0]['mean_change'] > pilot.runs[1]['mean_change']
                async with httpx.AsyncClient() as client:
                    response = await client.get(base + '/api/csi/recordings/' + pilot.runs[0]['id'])
                    assert response.status_code == 200 and 'normalized_change' in response.text
                await page.evaluate("""() => { const p=document.createElement('p'); p.textContent='ISOLATED QA · synthetic loopback UDP · accelerated clock · not physical Wi-Fi sensing'; p.style='background:#f7ecd4;padding:12px;text-align:center;margin:0'; document.getElementById('sensing').prepend(p); }""")
                await page.locator('#sensing').screenshot(path=str(ROOT/'artifacts/csi-synthetic-desktop.png'))
                await page.set_viewport_size({'width':390,'height':844})
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                dropping = True
                await expect(page.locator('#csi-state')).to_have_text('No data', timeout=10000)
                await expect(page.locator('#security-state')).to_have_text('Unavailable', timeout=10000)
                await expect(page.locator('#csi-calibrate')).to_be_disabled()
                await page.locator('#security-disarm').click()
                await expect(page.locator('#security-state')).to_have_text('Disarmed', timeout=10000)
                await page.locator('#csi-stop').click()
                await expect(page.locator('#csi-state')).to_have_text('Stopped', timeout=10000)
                assert not errors, errors
                print(json.dumps({'synthetic_loopback_udp_ui':'passed', 'calibration':True,
                    'empty_walking_csv':True, 'stale_data_is_unavailable':True,
                    'experimental_security_arm_incident_ack_and_degraded_state':True,
                    'mobile_layout':True, 'presentation_test_panel_removed':True, 'javascript_errors':errors}))
                await browser.close()
        finally:
            done = True
            if producer: await producer
            udp.close(); server.should_exit = True; await server_task


asyncio.run(main())
