import time

import pytest

from server.app import CameraHub, Event


def event(identifier, uptime, phase, boot="timing-boot", kind="motion"):
    return {"device_id": "timing-device", "boot_id": boot, "id": identifier,
            "uptime_ms": uptime, "phase": phase, "type": kind}


@pytest.mark.asyncio
async def test_duration_pairing_deduplication_out_of_order_and_reboot(tmp_path):
    hub = CameraHub(tmp_path)
    try:
        # A replay/fallback may deliver the clear before the start.
        hub.save_event(event(2, 7500, "cleared"), source="mqtt")
        assert hub.events()[0]["duration_ms"] is None
        hub.save_event(event(1, 1000, "triggered"), source="mqtt")
        hub.save_event(event(2, 7500, "cleared"))
        rows = hub.events()
        assert len(rows) == 2 and {row["duration_ms"] for row in rows} == {6500}
        assert next(row for row in rows if row["phase"] == "cleared")["start_event_id"] == 1
        hub.save_event(event(1, 100, "triggered", boot="other-boot"))
        assert hub.events()[0]["duration_ms"] is None
        hub.save_event(event(3, 0xfffffff0, "triggered", kind="sound"))
        hub.save_event(event(4, 0x10, "cleared", kind="sound"))
        assert hub.events()[0]["duration_ms"] == 32
        first_received = next(r["received_at"] for r in hub.events() if r["id"] == 1 and r["boot_id"] == "timing-boot")
        hub.save_event(event(1, 1000, "triggered"))
        assert next(r["received_at"] for r in hub.events() if r["id"] == 1 and r["boot_id"] == "timing-boot") == first_received
        # A trigger saved before the schema update can still acquire its end.
        old = event(1, 200, "triggered", boot="migration-boot"); old.pop("phase")
        hub.save_event(old); hub.save_event(event(2, 450, "cleared", boot="migration-boot"))
        assert all(r["duration_ms"] == 250 for r in hub.events() if r["boot_id"] == "migration-boot")
    finally:
        await hub.close()
    reopened = CameraHub(tmp_path)
    try:
        assert next(r for r in reopened.events() if r["id"] == 2 and r["boot_id"] == "timing-boot")["duration_ms"] == 6500
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_active_duration_stale_status_and_legacy_records(tmp_path):
    hub = CameraHub(tmp_path)
    try:
        hub.online = True
        hub.status = {"device_id": "timing-device", "boot_id": "timing-boot", "uptime_ms": 2000,
                      "sensor_timing_version": 1, "sensor_levels": {
                          "motion": {"level": 1, "active_level": 1, "level_for_ms": 1000,
                                     "active_event_id": 1, "trigger_count": 1},
                          "sound": {"level": 1, "active_level": 0, "level_for_ms": 500,
                                    "active_event_id": 0, "trigger_count": 0}}}
        hub.mark_seen()
        hub.save_event(event(1, 1000, "triggered"))
        assert hub.events()[0]["active"] and 1000 <= hub.events()[0]["duration_ms"] < 1200
        assert hub.sensor_activity()["sound"]["active_level"] == 0
        hub.last_seen_at = time.monotonic() - 30
        assert hub.sensor_activity()["stale"] and hub.sensor_activity()["motion"]["level_for_ms"] is None
        assert not hub.events()[0]["active"] and hub.events()[0]["duration_ms"] is None
        hub.save_event({k: v for k, v in event(5, 3000, "triggered").items() if k != "phase"})
        assert hub.events()[0]["phase"] is None and hub.events()[0]["duration_ms"] is None
        with pytest.raises(ValueError):
            Event.model_validate(event(6, 4000, "invalid"))
    finally:
        await hub.close()
