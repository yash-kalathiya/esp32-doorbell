"""Recovery fault injection is synthetic; it is not a physical interruption test."""
import asyncio
import json
import time

import httpx
import pytest
from fastapi import HTTPException

from server.app import CameraHub


@pytest.mark.asyncio
async def test_reboot_invalidates_cached_images_and_preserves_history(tmp_path):
    hub = CameraHub(tmp_path)
    try:
        hub.status = {'device_id':'synthetic', 'boot_id':'a'}; hub.mark_seen()
        hub.frame = b'old'; hub.snapshot_frame = (hub.settings.camera_url,time.monotonic(),b'old')
        hub.face_overlay = {'captured_at':time.monotonic()}
        hub.save_event({'device_id':'synthetic','boot_id':'a','id':1,'type':'sound','uptime_ms':3000})
        hub.status = {'device_id':'synthetic','boot_id':'b'}; hub.mark_seen()
        assert hub.frame is None and hub.snapshot_frame is None and hub.face_overlay is None
        assert len(hub.events()) == 1
    finally:
        await hub.close()


@pytest.mark.asyncio
async def test_csi_rejects_video_and_manual_video_requires_confirmed_csi_stop(tmp_path):
    commands = []
    fail_stop = False
    def camera(request):
        commands.append(request.url.path)
        if request.url.path == '/api/csi/config':
            if fail_stop: return httpx.Response(503)
            return httpx.Response(200,json={'device_id':'synthetic','boot_id':'a','csi_pilot':{'enabled':False,'supported':True}})
        return httpx.Response(200,json={'device_id':'synthetic','boot_id':'a','manual_live':True})
    hub = CameraHub(tmp_path,httpx.MockTransport(camera))
    hub.online = True
    hub.status = {'device_id':'synthetic','boot_id':'a','csi_pilot':{'supported':True,'enabled':False}}
    hub.csi.port = 1234
    try:
        hub.viewers = 1
        with pytest.raises(HTTPException) as exc: await hub.configure_csi(True)
        assert exc.value.status_code == 409 and not commands
        hub.viewers = 0
        hub.status['csi_pilot']['enabled'] = True
        with pytest.raises(HTTPException) as exc: await hub.camera_snapshot(hub.settings.camera_url)
        assert exc.value.status_code == 409 and not commands
        fail_stop = True
        with pytest.raises(HTTPException) as exc: await hub.command('start')
        assert exc.value.status_code == 502 and '/api/live/start' not in commands
        fail_stop = False
        await hub.command('start')
        assert commands[-2:] == ['/api/csi/config','/api/live/start']
    finally:
        hub.viewers = 0
        await hub.close()


@pytest.mark.asyncio
async def test_csi_stops_on_camera_reboot_and_never_restarts_itself(tmp_path):
    hub = CameraHub(tmp_path)
    hub.csi.activate('192.168.12.140','a',0)
    hub.online = True
    hub.status = {'device_id':'synthetic','boot_id':'b','csi_pilot':{'enabled':False,'reason':'Disabled at boot'}}
    task = asyncio.create_task(hub.maintain_csi())
    try:
        await asyncio.sleep(.01)
        assert not hub.csi.enabled
        assert 'calibrate again' in hub.csi.reason
        assert hub.csi.threshold is None
    finally:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        await hub.close()


@pytest.mark.asyncio
async def test_offline_viewers_do_not_hammer_camera_and_recovery_reuses_producer(tmp_path):
    requests = []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b'\xff\xd8recovered\xff\xd9'
                await asyncio.sleep(.01)
    def camera(request):
        requests.append(request.url.path)
        return httpx.Response(200,headers={'content-type':'multipart/x-mixed-replace'},stream=Stream())
    hub = CameraHub(tmp_path,httpx.MockTransport(camera))
    hub.status = {'boot_id':'a','stream_allowed':True}; hub.online = False
    first, second = hub.frames(), hub.frames()
    waiting = [asyncio.create_task(anext(first)),asyncio.create_task(anext(second))]
    try:
        await asyncio.sleep(.05)
        assert hub.viewers == 2 and requests == []
        hub.online = True
        frames = await asyncio.wait_for(asyncio.gather(*waiting),2)
        assert frames[0] == frames[1] and requests == ['/api/stream']
    finally:
        for task in waiting:
            if not task.done(): task.cancel()
        await asyncio.gather(*waiting,return_exceptions=True)
        await first.aclose(); await second.aclose(); await hub.close()
