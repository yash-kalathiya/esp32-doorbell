import json
import time
from types import SimpleNamespace

import pytest

from server.app import CameraHub


@pytest.mark.asyncio
async def test_mqtt_deduplication_receipt_after_commit_and_state_freshness(tmp_path):
    hub = CameraHub(tmp_path)
    bridge = hub.mqtt
    bridge.device_id, bridge.base, bridge.connected = "qa-device", "doorbell/qa-device", True
    published = []
    def publish(topic, payload, qos, retain):
        if topic.endswith("/received"):
            assert len(hub.events()) == 1  # Event has committed before receipt.
        published.append((topic, json.loads(payload), qos, retain))
        return SimpleNamespace(rc=0)
    bridge.client = SimpleNamespace(publish=publish)
    state = {"device_id":"qa-device", "online":True, "boot_id":"1234abcd", "uptime_ms":1000}
    try:
        await bridge.handle(bridge.base + "/state", json.dumps(state), retained=True)
        assert not bridge.ready()  # Historical retained-online state is not liveness.
        await bridge.handle(bridge.base + "/state", json.dumps(state))
        assert bridge.ready()
        event = {"device_id":"qa-device", "boot_id":"1234abcd", "id":1, "type":"motion", "uptime_ms":1000}
        await bridge.handle(bridge.base + "/events", json.dumps(event))
        await bridge.handle(bridge.base + "/events", json.dumps(event))
        assert len(hub.events()) == 1 and bridge.events_received == 2
        assert hub.events()[0]["source"] == "mqtt"
        assert len(published) == 2
        assert all(qos == 1 and not retain for _, _, qos, retain in published)
        bridge.last_state_at = time.monotonic() - 21
        assert not bridge.ready()
        await bridge.handle(bridge.base + "/state", json.dumps({**state, "online":False}), retained=True)
        assert not bridge.device_online
    finally:
        bridge.client = None
        await hub.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("topic,data,retained", [
    ("doorbell/other/events", {"device_id":"other"}, False),
    ("doorbell/qa-device/events", {"device_id":"qa-device", "type":"sound"}, True),
    ("doorbell/qa-device/events", {"device_id":"qa-device", "type":"unknown"}, False),
    ("doorbell/qa-device/state", {"device_id":"qa-device", "online":True, "boot_id":"bad"}, False),
    ("doorbell/qa-device/state", {"device_id":"qa-device", "online":"true"}, False),
])
async def test_invalid_mqtt_messages_never_become_events(tmp_path, topic, data, retained):
    hub = CameraHub(tmp_path)
    bridge = hub.mqtt
    bridge.device_id, bridge.base = "qa-device", "doorbell/qa-device"
    try:
        with pytest.raises((ValueError, KeyError)):
            await bridge.handle(topic, json.dumps(data), retained)
        assert hub.events() == []
    finally:
        await hub.close()


@pytest.mark.asyncio
async def test_http_event_schema_migrates_without_losing_history(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "events.sqlite3")
    db.execute("CREATE TABLE events (camera_url TEXT, device_id TEXT, boot_id TEXT, id INTEGER, type TEXT, uptime_ms INTEGER, received_at TEXT, PRIMARY KEY(camera_url,device_id,boot_id,id))")
    db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)", ("http://192.168.12.140", "old-device", "boot", 1, "motion", 100, "original-time"))
    db.commit(); db.close()
    hub = CameraHub(tmp_path)
    try:
        assert hub.events()[0]["received_at"] == "original-time"
        assert hub.events()[0]["source"] == "http"
    finally:
        await hub.close()
