"""LAN dashboard: one ESP32 stream, shared with all connected browsers."""
import asyncio
import base64
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
import ipaddress
import json
import logging
import os
from pathlib import Path
import socket
import sqlite3
import time
from urllib.parse import urlsplit
from typing import Literal

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from server.faces import (FaceStore, MATCH_THRESHOLD, BURST_IDENTITY_THRESHOLD,
                          BURST_DUPLICATE_THRESHOLD, OpenCVEngine, normalized, prepare_photo, validate_name)
from server.mqtt_bridge import MQTTBridge
from server.lighting import AutomaticLight, frame_brightness
from server.csi import CSIPilot
from server.security import SecurityMonitor

ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


class Settings(BaseModel):
    camera_url: str = "http://192.168.12.140"
    face_monitoring: bool = True
    night_mode: bool = False

    @field_validator("camera_url")
    @classmethod
    def local_camera(cls, value):
        value = value.strip().rstrip("/")
        if "://" not in value:
            value = "http://" + value
        try:
            url = urlsplit(value)
            address = ipaddress.IPv4Address(url.hostname or "")
            if (url.scheme != "http" or not address.is_private or address.is_multicast
                    or address.is_unspecified or address.is_link_local
                    or url.username or url.password or url.path or url.query
                    or url.fragment or url.port not in (None, 80)):
                raise ValueError()
        except ValueError:
            raise ValueError("Enter a local IPv4 camera address, such as 192.168.12.140 (HTTP port 80).")
        return f"http://{address}"


class Event(BaseModel):
    device_id: str
    boot_id: str
    id: int
    type: str
    uptime_ms: int
    phase: Literal["triggered", "cleared"] | None = None

    @field_validator("device_id", "boot_id", "type")
    @classmethod
    def bounded_text(cls, value):
        if not 1 <= len(value) <= 80:
            raise ValueError("Invalid event field length")
        return value

    @field_validator("id", "uptime_ms")
    @classmethod
    def nonnegative(cls, value):
        if value < 0:
            raise ValueError("Event numbers must be nonnegative")
        return value


class Enrollment(BaseModel):
    name: str = Field(min_length=1, max_length=80)


async def read_photo(file):
    if (file.size or 0) > 8 * 1024 * 1024:
        raise ValueError("Use a photo smaller than 8 MB.")
    data = await file.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("Use a photo smaller than 8 MB.")
    return await asyncio.to_thread(prepare_photo, data)


class CameraHub:
    def __init__(self, data_dir, transport=None, poll_interval=2, face_engine=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        config = self.data_dir / "settings.json"
        self.settings = Settings.model_validate_json(config.read_text()) if config.exists() else Settings()
        self.db = sqlite3.connect(self.data_dir / "events.sqlite3")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (camera_url TEXT, device_id TEXT, boot_id TEXT, id INTEGER, type TEXT, uptime_ms INTEGER, received_at TEXT, PRIMARY KEY(camera_url,device_id,boot_id,id))")
        if "source" not in [column[1] for column in self.db.execute("PRAGMA table_info(events)")]:
            self.db.execute("ALTER TABLE events ADD COLUMN source TEXT NOT NULL DEFAULT 'http'")
            self.db.commit()
        if "phase" not in [column[1] for column in self.db.execute("PRAGMA table_info(events)")]:
            self.db.execute("ALTER TABLE events ADD COLUMN phase TEXT")
            self.db.commit()
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(4, connect=2), transport=transport, trust_env=False)
        self.security = SecurityMonitor(self.db)
        self.csi = CSIPilot(self.data_dir)
        self.csi_lock = asyncio.Lock()
        self.csi_task = None
        self.poll_interval = poll_interval
        self.status = None
        self.online = False
        self.last_seen = None
        self.last_seen_at = 0
        self.error = "Waiting for the first camera check."
        self.boot_id = None
        self.observed_boot = None
        self.cursor = 0
        self.events_lost = 0
        self.condition = asyncio.Condition()
        self.frame = None
        self.frame_id = 0
        self.last_frame_at = 0
        self.stream_error = None
        self.viewers = 0
        self.producer = None
        self.poller = None
        self.control_lock = asyncio.Lock()
        self.faces = FaceStore(self.data_dir)
        self.face_engine = face_engine
        self.face_lock = asyncio.Lock()
        self.upload_lock = asyncio.Lock()
        self.face_task = None
        self.face_state = "loading"
        self.face_error = None
        self.face_last_checked = None
        self.face_count = 0
        self.face_rejected = 0
        self.face_overlay = None
        self.light = AutomaticLight()
        self.light_sampled_at = 0
        self.snapshot_lock = asyncio.Lock()
        self.snapshot_frame = None
        self.camera_controls_task = None
        self.camera_controls_key = None
        self.camera_controls_error = None
        self.config_generation = 0
        self.changed = asyncio.Event()
        self.mqtt = MQTTBridge(self, self.data_dir / "mqtt/gateway.json")

    timestamp = staticmethod(now)

    def mark_seen(self):
        boot = ((self.status or {}).get("device_id"), (self.status or {}).get("boot_id"))
        if self.observed_boot is not None and boot != self.observed_boot:
            self.frame = self.snapshot_frame = self.face_overlay = None
        self.observed_boot = boot
        self.last_seen, self.last_seen_at = now(), time.monotonic()

    def csi_active(self):
        return self.csi.enabled or (self.status or {}).get("csi_pilot", {}).get("enabled", False)

    async def camera_snapshot(self, base, max_age=2):
        if self.csi_active():
            raise HTTPException(409, "Stop the CSI pilot before taking camera photos.")
        # Face monitoring and lighting share one idle snapshot, with no new ESP32
        # frame buffer and no second live stream.
        async with self.snapshot_lock:
            if self.csi_active():
                raise HTTPException(409, "Stop the CSI pilot before taking camera photos.")
            cached = self.snapshot_frame
            if cached and cached[0] == base and time.monotonic() - cached[1] < max_age:
                return cached[2]
            response = await self.client.get(base + "/api/snapshot", headers={"Connection": "close"})
            response.raise_for_status()
            self.snapshot_frame = (base, time.monotonic(), response.content)
            return response.content

    def controls_key(self):
        return (self.settings.camera_url, (self.status or {}).get("boot_id"), self.settings.night_mode, self.light.enabled)

    async def apply_camera_controls(self, base, night, light):
        if "night_mode" in (self.status or {}):
            for group, enabled in (("night", night), ("light", light)):
                response = await self.client.post(base + f"/api/{group}/" + ("start" if enabled else "stop"), headers={"Connection": "close"})
                response.raise_for_status()
                state = response.json()
                if not isinstance(state, dict) or (group == "night" and state.get("night_mode") != night) or (group == "light" and state.get("camera_light_percent") != (50 if light else 0)):
                    raise ValueError("Camera did not confirm requested lighting settings")
            return
        for name, value in (("led_intensity", 128 if light else 0), ("aec", 1), ("agc", 1),
                            ("brightness", 1 if night else 0), ("ae_level", 1 if night else 0)):
            response = await self.client.get(base + "/control", params={"var": name, "val": value},
                                             headers={"Connection": "close"})
            response.raise_for_status()

    async def maintain_camera_controls(self):
        retry_key, retry_at, device = None, 0, None
        while True:
            current_device = (self.settings.camera_url, (self.status or {}).get("boot_id"))
            if current_device != device:
                self.light = AutomaticLight()
                self.light_sampled_at, device = 0, current_device
            if self.online and not self.csi_active() and current_device[1] and time.monotonic() - self.light_sampled_at >= 3:
                base = self.settings.camera_url
                try:
                    jpeg = self.frame if self.frame is not None and time.monotonic() - self.last_frame_at < 2 else await self.camera_snapshot(base)
                    brightness = await asyncio.to_thread(frame_brightness, jpeg)
                    if current_device == (self.settings.camera_url, (self.status or {}).get("boot_id")):
                        self.light.update(brightness, time.monotonic())
                except (httpx.HTTPError, HTTPException, ValueError, OSError):
                    # Unknown/stale brightness never forces the lamp on.
                    self.light.samples = 0
                self.light_sampled_at = time.monotonic()
            key = self.controls_key()
            if self.online and not self.csi_active() and key[1] and key != self.camera_controls_key and (key != retry_key or time.monotonic() >= retry_at):
                try:
                    await self.apply_camera_controls(key[0], key[2], key[3])
                    if self.controls_key() == key:
                        self.camera_controls_key, self.camera_controls_error = key, None
                except (httpx.HTTPError, ValueError):
                    self.camera_controls_error = "Camera lighting settings could not be applied; retrying when available."
                    retry_key, retry_at = key, time.monotonic() + 10
                self.changed.set()
            await asyncio.sleep(1)

    def sensor_activity(self):
        camera = self.status or {}
        levels = camera.get("sensor_levels")
        if camera.get("sensor_timing_version") != 1 or not isinstance(levels, dict):
            return {"supported": False}
        age = int((time.monotonic() - self.last_seen_at) * 1000) if self.last_seen_at else 20000
        fresh = self.online and 0 <= age < 15000
        result = {"supported": True, "stale": not fresh}
        for kind in ("motion", "sound"):
            level = levels.get(kind)
            if isinstance(level, dict):
                result[kind] = {**level, "level_for_ms": level.get("level_for_ms", 0) + age if fresh else None}
        return result

    async def discover_camera(self, ip):
        try:
            settings = self.settings.model_copy(update={"camera_url": Settings.local_camera(f"http://{ip}")})
            if settings.camera_url != self.settings.camera_url:
                await self.configure(settings)
        except ValueError:
            logging.getLogger(__name__).warning("MQTT device reported an invalid camera IP")

    def face_status(self):
        overlay = self.face_overlay
        remaining = 5000 - int((time.monotonic() - overlay["captured_at"]) * 1000) if overlay else 0
        if (remaining <= 0 or not self.settings.face_monitoring or self.face_error or not self.online
                or self.frame is None or time.monotonic() - self.last_frame_at >= 2
                or (overlay and overlay["boot_id"] != (self.status or {}).get("boot_id"))):
            overlay = None
        return {"model_state": self.face_state, "enabled": self.settings.face_monitoring,
                "camera_online": self.online, "error": self.face_error,
                "last_checked": self.face_last_checked, "faces_in_last_check": self.face_count,
                "low_quality_faces": self.face_rejected,
                "enrollment": {"recommended_photos": [5, 10], "max_samples": None,
                               "burst_frames": 8, "orientation_degrees": [0, 90, 180, 270], "detection_scales": [320, 640, 960]},
                "live_detections": ({**overlay["data"], "valid_for_ms": remaining} if overlay else None),
                **self.faces.gallery(self.settings.camera_url)}

    async def detect_faces(self, image):
        # Background checks and uploads share stateful OpenCV objects.
        async with self.face_lock:
            return await asyncio.to_thread(self.face_engine.detect, image)

    async def recognize_faces(self):
        try:
            if self.face_engine is None:
                directory = os.environ.get("DOORBELL_MODELS_DIR", ROOT / "models")
                self.face_engine = await asyncio.to_thread(OpenCVEngine, directory)
            self.face_state = "ready"
        except Exception as exc:
            self.face_state = "error"
            self.face_error = str(exc) if isinstance(exc, ValueError) else "Could not load face models. Check the server logs."
            logging.getLogger(__name__).exception("Face model initialization failed")
            return
        previous_frame = -1
        last_observed_at = 0
        while True:
            if self.settings.face_monitoring and self.online and not self.csi_active():
                base, generation = self.settings.camera_url, self.config_generation
                try:
                    receiving = self.producer is not None and self.frame is not None and time.monotonic() - self.last_frame_at < 5
                    if receiving:
                        if self.frame_id == previous_frame:
                            await asyncio.sleep(1)
                            continue
                        jpeg, previous_frame = self.frame, self.frame_id
                        captured_at = self.last_frame_at
                        captured_boot = (self.status or {}).get("boot_id")
                    else:
                        jpeg = await self.camera_snapshot(base)
                    result = await self.detect_faces(jpeg)
                    if generation == self.config_generation and self.settings.face_monitoring:
                        boxes = []
                        save_observations = time.monotonic() - last_observed_at >= 3
                        for face in result["faces"]:
                            if save_observations:
                                self.faces.observe(face, base)
                            if face.get("box"):
                                boxes.append({"box": face["box"], **self.faces.identify(face["embedding"]), "quality": "usable"})
                        if save_observations and result["faces"]:
                            last_observed_at = time.monotonic()
                        for face in result.get("rejected_boxes", []):
                            boxes.append({"box": face["box"], "label": {"too_small": "Move closer", "blurry": "Improve lighting", "uncertain": "Face candidate"}.get(face["reason"], "Face candidate"),
                                          "matched": False, "quality": face["reason"]})
                        self.face_overlay = ({"captured_at": captured_at, "boot_id": captured_boot,
                            "data": {"frame_id": previous_frame, "image_width": result["image_width"],
                                     "image_height": result["image_height"], "faces": boxes}}
                            if receiving and result.get("image_width") and result.get("image_height") else None)
                        self.face_last_checked = now()
                        self.face_count = len(result["faces"])
                        self.face_rejected = result["rejected"]
                        self.face_error = None
                except Exception:
                    self.face_overlay = None
                    if generation == self.config_generation:
                        self.face_error = "Face check failed. Retrying when a readable camera image is available."
                    logging.getLogger(__name__).warning("Face check failed", exc_info=True)
            await asyncio.sleep(1 if self.viewers else 3)

    def save_event(self, event, camera_url=None, source="http"):
        e = Event.model_validate(event)
        base = camera_url or self.settings.camera_url
        self.db.execute("INSERT OR IGNORE INTO events(camera_url,device_id,boot_id,id,type,uptime_ms,received_at,source,phase) VALUES (?,?,?,?,?,?,?,?,?)", (
            base, e.device_id, e.boot_id, e.id, e.type, e.uptime_ms, now(), source, e.phase))
        self.db.execute("UPDATE events SET phase=COALESCE(phase,?) WHERE camera_url=? AND device_id=? AND boot_id=? AND id=?", (e.phase, base, e.device_id, e.boot_id, e.id))
        self.db.execute("UPDATE events SET source='mqtt+http' WHERE camera_url=? AND device_id=? AND boot_id=? AND id=? AND source<>?", (base, e.device_id, e.boot_id, e.id, source))
        # Bound local retention; duplicates keep their original first-received time.
        self.db.execute("DELETE FROM events WHERE rowid NOT IN (SELECT rowid FROM events ORDER BY rowid DESC LIMIT 2000)")
        self.db.commit()
        self.changed.set()

    def events(self):
        rows = self.db.execute("""WITH ordered AS (
            SELECT rowid AS stored_order,device_id,boot_id,id,type,uptime_ms,received_at,source,phase,
              LAG(id) OVER sensor AS previous_id,LAG(phase) OVER sensor AS previous_phase,
              LAG(uptime_ms) OVER sensor AS previous_ms,LEAD(phase) OVER sensor AS next_phase,
              LEAD(uptime_ms) OVER sensor AS next_ms
            FROM events WHERE camera_url=?
            WINDOW sensor AS (PARTITION BY device_id,boot_id,type ORDER BY id)
          ) SELECT device_id,boot_id,id,type,uptime_ms,received_at,source,phase,
                   previous_id,previous_phase,previous_ms,next_phase,next_ms
            FROM ordered ORDER BY stored_order DESC LIMIT 100""", (self.settings.camera_url,)).fetchall()
        result = []
        camera = self.status or {}
        activity = self.sensor_activity()
        for row in rows:
            item = dict(zip(("device_id", "boot_id", "id", "type", "uptime_ms", "received_at", "source", "phase"), row[:8]))
            previous_id, previous_phase, previous_ms, next_phase, next_ms = row[8:]
            item.update(duration_ms=None, active=False, start_event_id=None)
            if item["phase"] == "cleared" and previous_ms is not None and previous_phase in (None, "triggered"):
                item.update(duration_ms=(item["uptime_ms"] - previous_ms) & 0xffffffff, start_event_id=previous_id)
            elif item["phase"] in (None, "triggered") and next_phase == "cleared":
                item["duration_ms"] = (next_ms - item["uptime_ms"]) & 0xffffffff
            elif item["phase"] == "triggered" and item["boot_id"] == camera.get("boot_id") and item["device_id"] == camera.get("device_id"):
                level = activity.get(item["type"], {})
                if not activity.get("stale", True) and level.get("active_event_id") == item["id"]:
                    age = max(0, int((time.monotonic() - self.last_seen_at) * 1000))
                    item.update(active=True, duration_ms=((camera["uptime_ms"] - item["uptime_ms"]) & 0xffffffff) + age)
            result.append(item)
        return result

    def state(self):
        return {"online": self.online, "camera_url": self.settings.camera_url,
                "last_seen": self.last_seen, "error": self.error,
                "camera": self.status, "video": {"viewers": self.viewers,
                "receiving": self.producer is not None and self.frame is not None and time.monotonic() - self.last_frame_at < 5,
                "error": self.stream_error}, "events_lost": self.events_lost,
                "mqtt": self.mqtt.status(), "control_transport": "mqtt" if self.mqtt.ready() else "http",
                "sensor_activity": self.sensor_activity(), "csi": self.csi.status(),
                "security": self.security.status(self.csi.status(), self.online), "camera_controls": {
                    "night_mode": self.settings.night_mode, "light_mode": "auto",
                    "light_percent": 50 if self.light.enabled else 0,
                    "brightness": self.light.brightness,
                    "applied": self.online and self.camera_controls_key == self.controls_key(),
                    "error": self.camera_controls_error}}

    async def poll_once(self):
        base = self.settings.camera_url
        try:
            response = await self.client.get(base + "/api/status")
            response.raise_for_status()
            status = response.json()
            if not isinstance(status, dict) or "boot_id" not in status or "device_id" not in status:
                raise ValueError("The address did not return the doorbell firmware API.")
            if base != self.settings.camera_url:
                return
            self.status, self.online, self.last_seen, self.error = status, True, now(), None
            self.mark_seen()
            if status["boot_id"] != self.boot_id:
                self.boot_id, self.cursor = status["boot_id"], 0
            response = await self.client.get(base + "/api/events", params={"after": self.cursor})
            response.raise_for_status()
            batch = response.json()
            if base != self.settings.camera_url:
                return
            # A reboot between the status and event requests must reset the cursor.
            if batch["boot_id"] != self.boot_id:
                self.boot_id, self.cursor = batch["boot_id"], 0
            for event in batch.get("events", []):
                self.save_event(event, base)
            self.cursor = batch["next_after"]
            self.events_lost = max(self.events_lost, batch.get("lost_before_cursor", 0))
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            if base == self.settings.camera_url:
                self.online = False
                self.error = ("Camera unreachable. Check its address, power, and that it shares the laptop's Wi-Fi."
                              if isinstance(exc, httpx.HTTPError) else "Unexpected camera API response. Check the firmware and address.")

    async def poll(self):
        while True:
            if self.mqtt.ready():
                # Fresh MQTT state/events are primary; HTTP remains the fallback.
                self.changed.set()
                await asyncio.sleep(1)
                continue
            await self.poll_once()
            self.changed.set()
            await asyncio.sleep(self.poll_interval)

    async def command(self, action):
        async with self.control_lock:
            if action == "start" and self.csi_active():
                await self.configure_csi(False)
            if self.mqtt.ready():
                try:
                    self.status = await self.mqtt.command(action)
                    self.last_seen, self.online, self.error = now(), True, None
                    self.mark_seen()
                    self.changed.set()
                    return self.state()
                except (ConnectionError, TimeoutError, ValueError) as exc:
                    raise HTTPException(502, "MQTT command was not confirmed. Check device status before retrying.") from exc
            try:
                response = await self.client.post(self.settings.camera_url + "/api/live/" + action)
                response.raise_for_status()
                status = response.json()
                if not isinstance(status, dict) or "boot_id" not in status:
                    raise ValueError("Invalid camera status")
                self.status, self.online, self.last_seen, self.error = status, True, now(), None
                self.mark_seen()
                self.changed.set()
                return self.state()
            except (httpx.HTTPError, ValueError) as exc:
                raise HTTPException(502, "Camera did not respond. Check its power, IP address, and Wi-Fi.") from exc

    async def produce(self):
        retry_delay = 2
        try:
            while self.viewers:
                if self.csi_active() or (self.status is not None and (not self.online or not self.status.get("stream_allowed", True))):
                    self.frame = None
                    self.stream_error = "Video paused during CSI capture." if self.csi_active() else "Waiting for camera availability or activity."
                    await asyncio.sleep(1)
                    continue
                try:
                    host = urlsplit(self.settings.camera_url).hostname
                    async with self.client.stream("GET", f"http://{host}:81/api/stream", timeout=httpx.Timeout(12, connect=3)) as response:
                        response.raise_for_status()
                        if "multipart/" not in response.headers.get("content-type", ""):
                            raise ValueError("Not an MJPEG stream")
                        buffer = bytearray()
                        async for chunk in response.aiter_bytes():
                            buffer.extend(chunk)
                            # JPEG markers tolerate arbitrary multipart/chunk boundaries.
                            while True:
                                start = buffer.find(b"\xff\xd8")
                                if start < 0:
                                    buffer[:] = buffer[-1:]
                                    break
                                if start:
                                    del buffer[:start]
                                end = buffer.find(b"\xff\xd9", 2)
                                if end < 0:
                                    break
                                frame = bytes(buffer[:end + 2])
                                del buffer[:end + 2]
                                async with self.condition:
                                    self.frame = frame
                                    self.frame_id += 1
                                    self.last_frame_at = time.monotonic()
                                    self.stream_error = None
                                    retry_delay = 2
                                    self.condition.notify_all()
                            if len(buffer) > 4 * 1024 * 1024:
                                raise ValueError("Camera frame exceeded 4 MB")
                    self.stream_error = "Stream ended. Waiting for camera activity or a live request."
                except (httpx.HTTPError, ValueError):
                    self.stream_error = "Video unavailable. Close other direct camera streams, or try Start live again."
                self.frame = None
                await asyncio.sleep(retry_delay)
                retry_delay = min(15, retry_delay * 2)
        finally:
            self.frame = None

    async def frames(self):
        self.viewers += 1
        if self.producer is None:
            self.producer = asyncio.create_task(self.produce())
        previous = -1
        try:
            while True:
                async with self.condition:
                    await self.condition.wait_for(lambda: self.frame is not None and self.frame_id != previous)
                    frame, previous = self.frame, self.frame_id
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
        finally:
            self.viewers -= 1
            if self.viewers == 0:
                await self.stop_producer()

    async def stop_producer(self):
        task, self.producer = self.producer, None
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.frame = None

    async def configure(self, settings):
        async with self.control_lock:
            camera_changed = settings.camera_url != self.settings.camera_url
            if camera_changed:
                self.csi.deactivate("Camera address changed. Start a new pilot session.")
                await self.stop_producer()
            self.settings = settings
            self.config_generation += 1
            if camera_changed or not settings.face_monitoring:
                self.face_overlay = None
            target = self.data_dir / "settings.json"
            temporary = target.with_suffix(".tmp")
            temporary.write_text(settings.model_dump_json())
            temporary.replace(target)
            if camera_changed:
                self.status, self.online, self.last_seen = None, False, None
                self.boot_id, self.cursor, self.events_lost = None, 0, 0
                self.error = "Checking the new camera address…"
                self.stream_error = None
                self.face_last_checked, self.face_count, self.face_rejected = None, 0, 0
                if self.viewers:
                    self.producer = asyncio.create_task(self.produce())
            self.changed.set()

    def security_context(self):
        # Camera observations provide context only. They neither identify CSI
        # motion nor prove a directional entrance/exit or authorized occupancy.
        rows = self.faces.db.execute('''SELECT s.id,p.name,s.last_seen FROM sightings s
            LEFT JOIN people p ON p.id=s.person_id WHERE s.camera_url=?
            AND s.last_seen >= ? ORDER BY s.last_seen DESC LIMIT 3''',
            (self.settings.camera_url, datetime.fromtimestamp(time.time()-120, timezone.utc).isoformat())).fetchall()
        recent = [{'capture_id':r[0], 'label':r[1] or 'Unknown', 'last_seen':r[2]} for r in rows]
        return {'device_id': (self.status or {}).get('device_id'),
                'boot_id': (self.status or {}).get('boot_id'),
                'radio_geometry': self.csi.signature, 'recent_camera_observations': recent,
                'sensor_policy': (self.status or {}).get('sensor_policy', {}),
                'identity_association': 'unknown'}

    async def maintain_csi(self):
        while True:
            state = self.status or {}
            diagnostics = state.get("diagnostics", {})
            memory = diagnostics.get("memory", {})
            self.csi.context = {"video_receiving": self.producer is not None and self.frame is not None and time.monotonic() - self.last_frame_at < 5,
                "mqtt_online": self.mqtt.ready(), "firmware": state.get("firmware_version"),
                "internal_free_bytes": memory.get("internal_free_bytes"),
                "internal_min_free_bytes": memory.get("internal_min_free_bytes"),
                "stream_errors": diagnostics.get("camera", {}).get("stream_errors"),
                "wifi_disconnects": diagnostics.get("wifi", {}).get("disconnects"),
                "queue_drops": state.get("csi_pilot", {}).get("queue_drops"),
                "send_errors": state.get("csi_pilot", {}).get("send_errors")}
            if self.csi.enabled:
                if not self.online or state.get("boot_id") != self.csi.boot or not state.get("csi_pilot", {}).get("enabled"):
                    self.csi.deactivate(state.get("csi_pilot", {}).get("reason", "Camera stopped or reconnected.") + " Start a new pilot session and calibrate again.")
            self.csi.flush()
            self.security.tick(self.csi.status(), self.online, self.security_context)
            await asyncio.sleep(1)

    async def configure_csi(self, enabled):
        async with self.csi_lock:
            if not enabled:
                self.csi.deactivate()
            if enabled and (not self.online or not (self.status or {}).get("csi_pilot", {}).get("supported")):
                raise HTTPException(409, "Upload csi-pilot-1 to the ESP32, connect it, then start the pilot.")
            if enabled and self.csi.port is None:
                raise HTTPException(503, self.csi.error or "CSI receiver is unavailable.")
            if enabled and (self.viewers or self.producer is not None or (self.status or {}).get("manual_live")):
                raise HTTPException(409, "Stop live video before starting CSI. Video and CSI run separately to protect ESP32 memory.")
            base = self.settings.camera_url
            host = urlsplit(base).hostname
            local = None
            try:
                if enabled:
                    # Route lookup only: connect on a UDP socket sends no packet.
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                        route.connect((host, 80))
                        local = route.getsockname()[0]
                response = await self.client.post(base + "/api/csi/config", json={
                    "enabled": enabled, "host": local or "", "port": self.csi.port or 5005}, headers={"Connection": "close"})
                response.raise_for_status()
                state = response.json()
                if not isinstance(state, dict) or not state.get("boot_id") or state.get("csi_pilot", {}).get("enabled") is not enabled:
                    raise ValueError("CSI configuration was not confirmed")
                if base != self.settings.camera_url:
                    raise ValueError("Camera changed while configuring CSI")
                self.status = state
                self.online = True
                self.mark_seen()
                if enabled:
                    seed = state["csi_pilot"]["sequence_start"]
                    if not isinstance(seed, int) or not 0 <= seed <= 0xffffffff:
                        raise ValueError("Invalid CSI session")
                    self.csi.activate(host, state["boot_id"], seed)
            except (httpx.HTTPError, ValueError, KeyError, TypeError, OSError) as exc:
                raise HTTPException(502, "CSI settings were not confirmed by the ESP32. Check the connection and firmware.") from exc
            self.changed.set()
            return self.csi.status()

    async def close(self):
        if self.csi_task:
            self.csi_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.csi_task
        if self.csi.enabled:
            with suppress(httpx.HTTPError):
                await self.client.post(self.settings.camera_url + "/api/csi/config", json={"enabled": False}, timeout=1)
        self.csi.close()
        if self.camera_controls_task:
            self.camera_controls_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.camera_controls_task
        await self.mqtt.close()
        if self.face_task:
            self.face_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.face_task
        if self.poller:
            self.poller.cancel()
            with suppress(asyncio.CancelledError):
                await self.poller
        await self.stop_producer()
        await self.client.aclose()
        self.db.close()
        self.faces.close()


def create_app(data_dir=None, transport=None, poll_interval=2, face_engine=None, csi_port=None):
    @asynccontextmanager
    async def lifespan(app):
        hub = CameraHub(data_dir or os.environ.get("DOORBELL_DATA_DIR", ROOT / ".local-data"), transport, poll_interval, face_engine)
        app.state.hub = hub
        await hub.csi.listen(csi_port if csi_port is not None else (0 if transport is not None else int(os.environ.get("DOORBELL_CSI_PORT", "5005"))))
        hub.csi_task = asyncio.create_task(hub.maintain_csi())
        await hub.mqtt.start()
        hub.poller = asyncio.create_task(hub.poll())
        hub.face_task = asyncio.create_task(hub.recognize_faces())
        hub.camera_controls_task = asyncio.create_task(hub.maintain_camera_controls())
        yield
        await hub.close()

    app = FastAPI(title="Doorbell dashboard", lifespan=lifespan)

    @app.middleware("http")
    async def same_origin(request, call_next):
        if request.url.path in ("/api/people/upload", "/api/faces/test"):
            try:
                limit = 8 if request.url.path.endswith("/test") else 40
                if int(request.headers.get("content-length", "0")) > limit * 1024 * 1024 + 128 * 1024:
                    return JSONResponse({"detail": f"Upload up to {limit} MB of photos at a time."}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Invalid upload length."}, status_code=400)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
            if origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "Use the dashboard's own address for controls."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    async def index():
        return FileResponse(ROOT / "web/index.html")

    @app.get("/api/status")
    async def status(request: Request):
        return request.app.state.hub.state()

    @app.get("/api/updates")
    async def updates(request: Request):
        hub = request.app.state.hub
        async def messages():
            previous = {}
            heartbeat = time.monotonic()
            while not await request.is_disconnected():
                snapshots = {"status": hub.state(), "activity": {"events": hub.events()}, "faces": hub.face_status()}
                for kind, value in snapshots.items():
                    payload = json.dumps(value, separators=(",", ":"))
                    if payload != previous.get(kind):
                        previous[kind] = payload
                        yield f"event: {kind}\ndata: {payload}\n\n"
                if time.monotonic() - heartbeat > 10:
                    yield ": heartbeat\n\n"
                    heartbeat = time.monotonic()
                hub.changed.clear()
                try:
                    await asyncio.wait_for(hub.changed.wait(), timeout=1)
                except TimeoutError:
                    pass
        return StreamingResponse(messages(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.get("/api/csi")
    async def csi_status(request: Request):
        return request.app.state.hub.csi.status()

    @app.get("/api/security")
    async def security_status(request: Request):
        hub = request.app.state.hub
        return hub.security.status(hub.csi.status(), hub.online)

    @app.post("/api/security/{action}")
    async def security_action(action: str, request: Request):
        hub = request.app.state.hub
        if action == "disarm": hub.security.disarm()
        elif action == "arm":
            try: hub.security.arm(hub.csi.status(), hub.online)
            except ValueError as exc: raise HTTPException(409, str(exc)) from exc
        else: raise HTTPException(404, "Unknown security action")
        hub.changed.set()
        return hub.security.status(hub.csi.status(), hub.online)

    @app.post("/api/security/incidents/{identifier}/acknowledge")
    async def acknowledge_incident(identifier: str, request: Request):
        hub = request.app.state.hub
        try: hub.security.acknowledge(identifier)
        except KeyError as exc: raise HTTPException(404, "Incident not found") from exc
        hub.changed.set()
        return hub.security.status(hub.csi.status(), hub.online)

    @app.post("/api/csi/{action}")
    async def csi_action(action: str, request: Request):
        hub = request.app.state.hub
        if action in ("start", "stop"):
            return await hub.configure_csi(action == "start")
        try:
            if action == "calibrate": hub.csi.calibrate()
            elif action in ("record-empty", "record-walking"): hub.csi.begin_trial(action.removeprefix("record-"))
            else: raise HTTPException(404, "Unknown CSI action")
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        hub.changed.set()
        return hub.csi.status()

    @app.get("/api/csi/recordings/{identifier}")
    async def csi_recording(identifier: str, request: Request):
        if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
            raise HTTPException(404, "Recording not found")
        path = request.app.state.hub.csi.directory / f"{identifier}.csv"
        if not path.is_file(): raise HTTPException(404, "Recording not found")
        return FileResponse(path, media_type="text/csv", filename=f"csi-{identifier}.csv")

    @app.get("/api/events")
    async def events(request: Request):
        return {"events": request.app.state.hub.events()}

    @app.post("/api/events")
    async def receive_event(event: Event, request: Request):
        hub = request.app.state.hub
        if hub.status and event.device_id != hub.status.get("device_id"):
            raise HTTPException(409, "Event belongs to a different camera.")
        hub.save_event(event.model_dump())
        return {"accepted": True}

    @app.get("/api/network")
    async def network(request: Request):
        # UDP route lookup sends no packet. Re-evaluate when Wi-Fi changes.
        address = None
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            try:
                probe.connect(("192.168.0.1", 80))
                address = probe.getsockname()[0]
            except OSError:
                pass
        port = request.url.port or 80
        return {"phone_url": f"http://{address}:{port}" if address else None}

    @app.get("/api/settings")
    async def settings(request: Request):
        return request.app.state.hub.settings.model_dump()

    @app.get("/api/faces")
    async def faces(request: Request):
        return request.app.state.hub.face_status()

    @app.post("/api/faces/test")
    async def test_recognition(request: Request, file: UploadFile = File(...)):
        hub = request.app.state.hub
        try:
            if hub.face_state != "ready":
                raise HTTPException(503, "Face models are not ready. Wait for them to load or check the face-service status.")
            try:
                jpeg = await read_photo(file)
                result = await hub.detect_faces(jpeg)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            except Exception as exc:
                logging.getLogger(__name__).warning("Recognition test failed", exc_info=True)
                raise HTTPException(422, "Could not process this photo. Try another clear image.") from exc
            faces = []
            people = {person["id"]: person["name"] for person in hub.faces.gallery(hub.settings.camera_url)["people"]}
            for detection in result["faces"]:
                person_id, score = hub.faces.match(detection["embedding"])
                faces.append({"person_id": person_id, "name": people.get(person_id),
                    "matched": person_id is not None, "similarity": score,
                    "image_data_url": "data:image/jpeg;base64," + base64.b64encode(detection["thumbnail"]).decode("ascii")})
            return {"faces": faces, "low_quality_faces": result["rejected"],
                    "match_threshold": MATCH_THRESHOLD, "saved": False}
        finally:
            await file.close()

    @app.post("/api/people/upload")
    async def upload_person(request: Request, name: str = Form(...), files: list[UploadFile] = File(...),
                            burst: bool = Form(False), capture_id: str = Form("")):
        hub = request.app.state.hub
        try:
            try:
                name = validate_name(name)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            if not 1 <= len(files) <= 10:
                raise HTTPException(413, "Select between 1 and 10 photos per upload.")
            if sum(file.size or 0 for file in files) > 40 * 1024 * 1024:
                raise HTTPException(413, "Upload up to 40 MB of photos at a time.")
            if hub.face_state != "ready":
                raise HTTPException(503, "Face models are not ready. Wait for them to load or check the face-service status.")
            results, detections = [], []
            generation = hub.config_generation
            async with hub.upload_lock:
                anchor = None
                if capture_id:
                    if not burst:
                        raise HTTPException(400, "A reference capture is only accepted for burst enrollment.")
                    try:
                        anchor = hub.faces.enrollment_reference(capture_id, hub.settings.camera_url)
                    except KeyError as exc:
                        raise HTTPException(404, exc.args[0]) from exc
                    detections.append(anchor)
                reference = [anchor["embedding"]] if anchor else hub.faces.person_embeddings(name) if burst else []
                seen = hub.faces.person_embeddings(name) + [anchor["embedding"]] if anchor else list(reference)
                burst_added = 0
                for file in files:
                    item = {"filename": (file.filename or "Photo")[:120], "status": "rejected"}
                    try:
                        jpeg = await read_photo(file)
                        detected = await hub.detect_faces(jpeg)
                        count = len(detected["faces"]) + detected["rejected"]
                        if count > 1:
                            raise ValueError("More than one face found. Crop the photo to just this person.")
                        if not detected["faces"]:
                            reasons = detected.get("rejected_reasons", [])
                            raise ValueError(reasons[0] if reasons else "No face detected. Try a front-facing photo with the whole face visible and uncovered.")
                        face = detected["faces"][0]
                        vector = normalized(face["embedding"])
                        if burst:
                            if reference and max(float(vector @ other) for other in reference) < BURST_IDENTITY_THRESHOLD:
                                raise ValueError("Face does not match the reference person. Keep only this person in view.")
                            if any(float(vector @ other) >= BURST_DUPLICATE_THRESHOLD for other in seen):
                                item.update(status="skipped", reason="Near-duplicate view skipped; change your angle or distance slightly.")
                                results.append(item)
                                continue
                            if not reference:
                                reference = [vector]
                            seen.append(vector)
                            burst_added += 1
                        detections.append(face)
                        item.update(status="added", reason="Added to the profile.")
                    except ValueError as exc:
                        item["reason"] = str(exc)
                    except Exception:
                        logging.getLogger(__name__).warning("Uploaded face photo could not be processed", exc_info=True)
                        item["reason"] = "Could not process this photo. Try a different clear image."
                    results.append(item)
                if capture_id and generation != hub.config_generation:
                    raise HTTPException(409, "Camera settings changed during the burst. Capture it again.")
                if not detections:
                    if burst and reference and all(item["status"] == "skipped" for item in results):
                        person = hub.faces.db.execute("SELECT id,name,created_at FROM people WHERE name_key=?", (name.casefold(),)).fetchone()
                        if person is None:
                            raise HTTPException(409, "Profile changed during enrollment. Review the gallery and retry.")
                        person = dict(person)
                        return {"person": person, "samples_added": 0, "burst_frames_added": 0, "results": results}
                    reasons = list(dict.fromkeys(item["reason"] for item in results))
                    logging.getLogger(__name__).warning("Photo enrollment rejected %d photos: %s", len(results), " | ".join(reasons))
                    return JSONResponse({"detail": "No photos were added. " + " ".join(reasons), "results": results}, status_code=422)
                try:
                    person = hub.faces.enroll_photos(name, detections, capture_id or None)
                except KeyError as exc:
                    raise HTTPException(404, exc.args[0]) from exc
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
            hub.changed.set()
            return {"person": person, "samples_added": len(detections), "burst_frames_added": burst_added, "results": results}
        finally:
            for file in files:
                await file.close()

    @app.post("/api/faces/{capture_id}/enroll")
    async def enroll(capture_id: str, enrollment: Enrollment, request: Request):
        try:
            return request.app.state.hub.faces.enroll(capture_id, enrollment.name)
        except KeyError as exc:
            raise HTTPException(404, exc.args[0]) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/faces/{capture_id}/image")
    async def face_image(capture_id: str, request: Request):
        try:
            return Response(request.app.state.hub.faces.image(capture_id), media_type="image/jpeg")
        except KeyError as exc:
            raise HTTPException(404, "Face image not found") from exc

    @app.get("/api/people/{person_id}/image")
    async def person_image(person_id: str, request: Request):
        try:
            return Response(request.app.state.hub.faces.image(person_id, person=True), media_type="image/jpeg")
        except KeyError as exc:
            raise HTTPException(404, "Profile image not found") from exc

    @app.delete("/api/people/{person_id}")
    async def delete_person(person_id: str, request: Request):
        try:
            request.app.state.hub.faces.delete_person(person_id)
            return {"deleted": True}
        except KeyError as exc:
            raise HTTPException(404, "Profile not found") from exc

    @app.put("/api/settings")
    async def configure(settings: Settings, request: Request):
        await request.app.state.hub.configure(settings)
        return settings.model_dump()

    @app.post("/api/live/{action}")
    async def live(action: str, request: Request):
        if action not in ("start", "stop"):
            raise HTTPException(404, "Unknown control")
        return await request.app.state.hub.command(action)

    @app.get("/api/stream")
    async def stream(request: Request):
        return StreamingResponse(request.app.state.hub.frames(), media_type="multipart/x-mixed-replace; boundary=frame")

    @app.get("/api/enrollment/frame")
    async def enrollment_frame(request: Request):
        hub = request.app.state.hub
        if not hub.online:
            raise HTTPException(503, "ESP32 camera is offline. Connect it before capturing a burst.")
        base, generation = hub.settings.camera_url, hub.config_generation
        try:
            if hub.frame is not None and time.monotonic() - hub.last_frame_at < 2:
                jpeg = hub.frame
            else:
                jpeg = await hub.camera_snapshot(base, max_age=.25)
            if generation != hub.config_generation:
                raise HTTPException(409, "Camera changed during capture. Start the burst again.")
            if not jpeg.startswith(b"\xff\xd8") or len(jpeg) > 4 * 1024 * 1024:
                raise ValueError("Invalid camera JPEG")
            return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, "Could not capture a burst frame. Check the ESP32 connection.") from exc

    @app.get("/api/snapshot")
    async def snapshot(request: Request):
        hub = request.app.state.hub
        if hub.csi_active():
            raise HTTPException(409, "Stop the CSI pilot before taking camera photos.")
        try:
            response = await hub.client.get(hub.settings.camera_url + "/api/snapshot")
            response.raise_for_status()
            if not response.content.startswith(b"\xff\xd8"):
                raise ValueError("Not a JPEG")
            return Response(response.content, media_type="image/jpeg", headers={"Content-Disposition": 'attachment; filename="doorbell-snapshot.jpg"'})
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, "Could not capture a photo. Check the camera connection.") from exc

    app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
    return app


app = create_app()
