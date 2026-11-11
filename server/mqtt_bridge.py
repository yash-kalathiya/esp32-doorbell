"""MQTT 3.1.1 mutual TLS, durable-event acknowledgement, expiring commands."""
import asyncio
import json
import logging
from pathlib import Path
import re
import time
import uuid

import paho.mqtt.client as mqtt

LOG = logging.getLogger(__name__)


class MQTTBridge:
    def __init__(self, hub, config_path):
        self.hub = hub
        self.path = Path(config_path)
        self.configured = self.path.is_file()
        self.connected = False
        self.device_online = False
        self.last_state = None
        self.last_state_at = 0
        self.last_error = None
        self.events_received = 0
        self.rejected = 0
        self.client = None
        self.worker = None
        self.waiters = {}
        self.queue = asyncio.Queue(maxsize=256)
        self.closing = False
        self.device_id = None
        self.config = None

    def status(self):
        age = time.monotonic() - self.last_state_at if self.last_state_at else None
        return {"configured": self.configured, "broker_connected": self.connected,
                "device_id": self.device_id, "device_online": self.connected and self.device_online and age is not None and age < 20,
                "last_state_age_ms": round(age * 1000) if age is not None else None,
                "events_received": self.events_received, "messages_rejected": self.rejected,
                "error": self.last_error, "tls": self.client is not None}

    def ready(self):
        return self.status()["device_online"] and self.last_state is not None

    async def start(self):
        if not self.configured:
            return
        self.loop = asyncio.get_running_loop()
        try:
            self.config = json.loads(self.path.read_text())
            self.device_id = self.config["device_id"]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", self.device_id):
                raise ValueError("Invalid MQTT device ID")
            self.base = f"doorbell/{self.device_id}"
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                client_id="doorbell-gateway", clean_session=False, manual_ack=True)
            self.client.tls_set(ca_certs=self.config["ca"], certfile=self.config["certificate"], keyfile=self.config["key"])
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message
            self.client.reconnect_delay_set(1, 10)
            self.client.connect_async(self.config["host"], int(self.config["port"]), keepalive=15)
            self.worker = asyncio.create_task(self._consume())
            self.client.loop_start()
        except Exception:
            self.last_error = "MQTT configuration or certificate setup failed. Check the server logs."
            LOG.exception("MQTT startup failed")

    def _schedule(self, function, *args):
        if not self.closing:
            self.loop.call_soon_threadsafe(function, *args)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self._schedule(self._connection, False, "Broker rejected the client certificate or connection.")
            return
        client.subscribe([(self.base + "/events", 1), (self.base + "/state", 1), (self.base + "/ack", 1)])
        self._schedule(self._connection, True, None)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._schedule(self._connection, False, "MQTT broker disconnected; reconnecting.")

    def _connection(self, connected, error):
        self.connected, self.last_error = connected, error
        if not connected:
            self.device_online = False
        self.hub.changed.set()

    def _on_message(self, client, userdata, message):
        self._schedule(self._enqueue, message.topic, bytes(message.payload), message.mid, message.qos, message.retain)

    def _enqueue(self, topic, payload, mid, qos, retained):
        if len(payload) > 4096 or self.queue.full():
            self.rejected += 1
            # Invalid/overflow packets are not accepted as device events.
            self.client.ack(mid, qos)
            return
        self.queue.put_nowait((topic, payload, mid, qos, retained))

    def _publish(self, suffix, payload):
        info = self.client.publish(self.base + "/" + suffix, json.dumps(payload, separators=(",", ":")), qos=1, retain=False)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError("MQTT publication failed")

    async def _consume(self):
        while True:
            topic, payload, mid, qos, retained = await self.queue.get()
            try:
                await self.handle(topic, payload, retained)
                self.last_error = None
            except (ValueError, KeyError, TypeError):
                self.rejected += 1
                LOG.warning("Rejected malformed or mismatched MQTT message")
            except Exception:
                self.last_error = "MQTT message processing failed; device events will be retried."
                LOG.exception("MQTT message processing failed")
                # No application receipt is sent if database commit fails.
            finally:
                self.client.ack(mid, qos)
                self.queue.task_done()
                self.hub.changed.set()

    async def handle(self, topic, payload, retained=False):
        data = json.loads(payload)
        if not isinstance(data, dict) or data.get("device_id") != self.device_id or not topic.startswith(self.base + "/"):
            raise ValueError("Device identity mismatch")
        kind = topic[len(self.base) + 1:]
        if kind == "events":
            if retained or data.get("type") not in ("motion", "sound"):
                raise ValueError("Unexpected event")
            self.hub.save_event(data, source="mqtt")  # SQLite commits before app receipt.
            self.events_received += 1
            self._publish("received", {"boot_id": data["boot_id"], "id": data["id"]})
        elif kind == "state":
            if not isinstance(data.get("online"), bool):
                raise ValueError("Missing presence state")
            if data["online"]:
                # Retained online state is historical; require a fresh heartbeat.
                if retained:
                    return
                if not isinstance(data.get("uptime_ms"), int) or not re.fullmatch(r"[0-9a-f]{8}", data.get("boot_id", "")):
                    raise ValueError("Invalid device state")
                self.last_state, self.last_state_at = data, time.monotonic()
                self.device_online = True
                self.hub.status = data
                self.hub.last_seen = self.hub.timestamp()
                self.hub.online, self.hub.error = True, None
                # Certificate-authenticated device discovery handles DHCP changes.
                if data.get("ip"):
                    asyncio.create_task(self.hub.discover_camera(data["ip"]))
            else:
                self.device_online = False
        elif kind == "ack":
            future = self.waiters.get(data.get("command_id"))
            if future and not future.done():
                if data.get("boot_id") != self.last_state.get("boot_id"):
                    raise ValueError("Stale command acknowledgement")
                state = data.get("state")
                if data.get("ok") and (not isinstance(state, dict) or state.get("device_id") != self.device_id or state.get("boot_id") != data["boot_id"]):
                    raise ValueError("Invalid command state")
                future.set_result(data)
        else:
            raise ValueError("Unexpected topic")

    async def command(self, action):
        if not self.ready():
            raise ConnectionError("MQTT device state is unavailable or stale")
        identifier = uuid.uuid4().hex
        elapsed = max(0, int((time.monotonic() - self.last_state_at) * 1000))
        expiry = (self.last_state["uptime_ms"] + elapsed + 5000) & 0xffffffff
        payload = {"command_id": identifier, "action": action, "boot_id": self.last_state["boot_id"], "valid_until_uptime_ms": expiry}
        future = asyncio.get_running_loop().create_future()
        self.waiters[identifier] = future
        try:
            self._publish("commands", payload)
            result = await asyncio.wait_for(future, timeout=6)
            if not result.get("ok"):
                raise ValueError(result.get("error", "Device rejected command"))
            return result["state"]
        finally:
            self.waiters.pop(identifier, None)

    async def close(self):
        self.closing = True
        if self.client:
            self.client.disconnect()
            await asyncio.to_thread(self.client.loop_stop)
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
