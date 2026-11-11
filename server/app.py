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

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from server.faces import FaceStore, MATCH_THRESHOLD, OpenCVEngine, prepare_photo, validate_name
from server.mqtt_bridge import MQTTBridge

ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


class Settings(BaseModel):
    camera_url: str = "http://192.168.12.140"
    face_monitoring: bool = True

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
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(4, connect=2), transport=transport, trust_env=False)
        self.poll_interval = poll_interval
        self.status = None
        self.online = False
        self.last_seen = None
        self.error = "Waiting for the first camera check."
        self.boot_id = None
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
        self.config_generation = 0
        self.changed = asyncio.Event()
        self.mqtt = MQTTBridge(self, self.data_dir / "mqtt/gateway.json")

    timestamp = staticmethod(now)

    async def discover_camera(self, ip):
        try:
            settings = Settings(camera_url=f"http://{ip}", face_monitoring=self.settings.face_monitoring)
            if settings.camera_url != self.settings.camera_url:
                await self.configure(settings)
        except ValueError:
            logging.getLogger(__name__).warning("MQTT device reported an invalid camera IP")

    def face_status(self):
        return {"model_state": self.face_state, "enabled": self.settings.face_monitoring,
                "camera_online": self.online, "error": self.face_error,
                "last_checked": self.face_last_checked, "faces_in_last_check": self.face_count,
                "low_quality_faces": self.face_rejected,
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
        while True:
            if self.settings.face_monitoring and self.online:
                base, generation = self.settings.camera_url, self.config_generation
                try:
                    receiving = self.producer is not None and self.frame is not None and time.monotonic() - self.last_frame_at < 5
                    if receiving:
                        if self.frame_id == previous_frame:
                            await asyncio.sleep(1)
                            continue
                        jpeg, previous_frame = self.frame, self.frame_id
                    else:
                        response = await self.client.get(base + "/api/snapshot")
                        response.raise_for_status()
                        jpeg = response.content
                    result = await self.detect_faces(jpeg)
                    if generation == self.config_generation and self.settings.face_monitoring:
                        for face in result["faces"]:
                            self.faces.observe(face, base)
                        self.face_last_checked = now()
                        self.face_count = len(result["faces"])
                        self.face_rejected = result["rejected"]
                        self.face_error = None
                except Exception:
                    if generation == self.config_generation:
                        self.face_error = "Face check failed. Retrying when a readable camera image is available."
                    logging.getLogger(__name__).warning("Face check failed", exc_info=True)
            await asyncio.sleep(3)

    def save_event(self, event, camera_url=None, source="http"):
        e = Event.model_validate(event)
        base = camera_url or self.settings.camera_url
        self.db.execute("INSERT OR IGNORE INTO events(camera_url,device_id,boot_id,id,type,uptime_ms,received_at,source) VALUES (?,?,?,?,?,?,?,?)", (
            base, e.device_id, e.boot_id, e.id, e.type, e.uptime_ms, now(), source))
        self.db.execute("UPDATE events SET source='mqtt+http' WHERE camera_url=? AND device_id=? AND boot_id=? AND id=? AND source<>?", (base, e.device_id, e.boot_id, e.id, source))
        # Bound local retention; duplicates keep their original first-received time.
        self.db.execute("DELETE FROM events WHERE rowid NOT IN (SELECT rowid FROM events ORDER BY rowid DESC LIMIT 2000)")
        self.db.commit()
        self.changed.set()

    def events(self):
        rows = self.db.execute("SELECT device_id,boot_id,id,type,uptime_ms,received_at,source FROM events WHERE camera_url=? ORDER BY rowid DESC LIMIT 100", (self.settings.camera_url,)).fetchall()
        return [dict(zip(("device_id", "boot_id", "id", "type", "uptime_ms", "received_at", "source"), row)) for row in rows]

    def state(self):
        return {"online": self.online, "camera_url": self.settings.camera_url,
                "last_seen": self.last_seen, "error": self.error,
                "camera": self.status, "video": {"viewers": self.viewers,
                "receiving": self.producer is not None and self.frame is not None and time.monotonic() - self.last_frame_at < 5,
                "error": self.stream_error}, "events_lost": self.events_lost,
                "mqtt": self.mqtt.status(), "control_transport": "mqtt" if self.mqtt.ready() else "http"}

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
            if self.mqtt.ready():
                try:
                    self.status = await self.mqtt.command(action)
                    self.last_seen, self.online, self.error = now(), True, None
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
                self.changed.set()
                return self.state()
            except (httpx.HTTPError, ValueError) as exc:
                raise HTTPException(502, "Camera did not respond. Check its power, IP address, and Wi-Fi.") from exc

    async def produce(self):
        try:
            while self.viewers:
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
                                    self.condition.notify_all()
                            if len(buffer) > 4 * 1024 * 1024:
                                raise ValueError("Camera frame exceeded 4 MB")
                    self.stream_error = "Stream ended. Waiting for camera activity or a live request."
                except (httpx.HTTPError, ValueError):
                    self.stream_error = "Video unavailable. Close other direct camera streams, or try Start live again."
                self.frame = None
                await asyncio.sleep(2)
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
                await self.stop_producer()
            self.settings = settings
            self.config_generation += 1
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

    async def close(self):
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


def create_app(data_dir=None, transport=None, poll_interval=2, face_engine=None):
    @asynccontextmanager
    async def lifespan(app):
        hub = CameraHub(data_dir or os.environ.get("DOORBELL_DATA_DIR", ROOT / ".local-data"), transport, poll_interval, face_engine)
        app.state.hub = hub
        await hub.mqtt.start()
        hub.poller = asyncio.create_task(hub.poll())
        hub.face_task = asyncio.create_task(hub.recognize_faces())
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
    async def upload_person(request: Request, name: str = Form(...), files: list[UploadFile] = File(...)):
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
            async with hub.upload_lock:
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
                        detections.append(detected["faces"][0])
                        item.update(status="added", reason="Added to the profile.")
                    except ValueError as exc:
                        item["reason"] = str(exc)
                    except Exception:
                        logging.getLogger(__name__).warning("Uploaded face photo could not be processed", exc_info=True)
                        item["reason"] = "Could not process this photo. Try a different clear image."
                    results.append(item)
                if not detections:
                    reasons = list(dict.fromkeys(item["reason"] for item in results))
                    logging.getLogger(__name__).warning("Photo enrollment rejected %d photos: %s", len(results), " | ".join(reasons))
                    return JSONResponse({"detail": "No photos were added. " + " ".join(reasons), "results": results}, status_code=422)
                try:
                    person = hub.faces.enroll_photos(name, detections)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
            return {"person": person, "samples_added": len(detections), "results": results}
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

    @app.get("/api/snapshot")
    async def snapshot(request: Request):
        hub = request.app.state.hub
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
