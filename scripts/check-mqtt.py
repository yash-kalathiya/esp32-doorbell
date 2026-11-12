"""Real TLS broker test, simulated ESP32, isolated database and identities."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
import paho.mqtt.client as mqtt
from server.app import CameraHub


async def until(predicate, timeout=12):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.05)


async def main():
    spec = importlib.util.spec_from_file_location("provision", ROOT / "scripts/setup-local-mqtt.py")
    provision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(provision)
    name = "doorbell-mqtt-qa-" + uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix="doorbell-mqtt-qa-") as temporary:
        data = Path(temporary)
        directory = data / "mqtt"
        config = provision.provision(directory, "127.0.0.1", "qa-device")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        subprocess.run(["docker", "run", "-d", "--name", name, "--user", f"{os.getuid()}:{os.getgid()}",
            "--publish", f"127.0.0.1:{port}:8883", "--volume", f"{directory / 'broker'}:/mosquitto/config:rw,Z",
            "eclipse-mosquitto:2", "mosquitto", "-c", "/mosquitto/config/mosquitto.conf"], check=True, capture_output=True)
        hub, device = None, None
        heartbeat = None
        try:
            assert int(subprocess.check_output(["docker", "port", name, "8883/tcp"], text=True).strip().rsplit(":", 1)[1]) == port
            config["port"] = port
            (directory / "gateway.json").write_text(json.dumps(config))
            def offline(request):
                raise httpx.ConnectError("No real ESP32 in this test")
            hub = CameraHub(data, httpx.MockTransport(offline))
            await hub.mqtt.start()
            loop = asyncio.get_running_loop()
            connected = asyncio.Event()
            connection_count = 0
            receipts, command_ids = [], []
            manual = False
            started = time.monotonic()
            def uptime():
                return 65000 + int((time.monotonic() - started) * 1000)
            def state():
                return {"device_id": "qa-device", "boot_id": "1234abcd", "online": True,
                    "uptime_ms": uptime(), "manual_live": manual, "stream_allowed": manual,
                    "test_padding": "x" * 1300}
            def on_connect(client, userdata, flags, reason, properties):
                nonlocal connection_count
                if not reason.is_failure:
                    connection_count += 1
                    client.subscribe([("doorbell/qa-device/commands", 1), ("doorbell/qa-device/received", 1)])
                    loop.call_soon_threadsafe(connected.set)
            def on_message(client, userdata, message):
                nonlocal manual
                value = json.loads(message.payload)
                if message.topic.endswith("/received"):
                    receipts.append(value)
                    return
                command_ids.append(value["command_id"])
                remaining = value["valid_until_uptime_ms"] - uptime()
                ok = value["boot_id"] == "1234abcd" and 0 <= remaining <= 10000
                if ok:
                    manual = value["action"] == "start"
                client.publish("doorbell/qa-device/ack", json.dumps({"device_id":"qa-device", "boot_id":"1234abcd",
                    "command_id":value["command_id"], "ok":ok, "state":state()}), qos=1)
            device = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="qa-device")
            device.tls_set(ca_certs=str(directory / "ca.crt"), certfile=str(directory / "device.crt"), keyfile=str(directory / "device.key"))
            device.on_connect, device.on_message = on_connect, on_message
            device.will_set("doorbell/qa-device/state", json.dumps({"device_id":"qa-device", "boot_id":"1234abcd", "online":False}), qos=1, retain=True)
            device.connect_async("127.0.0.1", port, keepalive=5)
            device.loop_start()
            await until(lambda: connected.is_set() and hub.mqtt.connected)
            async def pulses():
                while True:
                    device.publish("doorbell/qa-device/state", json.dumps(state()), qos=0, retain=True)
                    await asyncio.sleep(.3)
            heartbeat = asyncio.create_task(pulses())
            await until(hub.mqtt.ready)
            assert len(json.dumps(state())) > 1024
            assert hub.mqtt.last_state["test_padding"] == "x" * 1300
            event = {"device_id":"qa-device", "boot_id":"1234abcd", "id":1, "type":"motion", "uptime_ms":uptime(), "phase":"triggered"}
            for _ in range(2):
                device.publish("doorbell/qa-device/events", json.dumps(event), qos=1)
            await until(lambda: len(receipts) == 2)
            assert len(hub.events()) == 1 and hub.events()[0]["source"] == "mqtt"
            first_received = hub.events()[0]["received_at"]
            hub.save_event(event)
            assert len(hub.events()) == 1 and hub.events()[0]["source"] == "mqtt+http"
            assert hub.events()[0]["received_at"] == first_received
            device.publish("doorbell/qa-device/events", json.dumps({**event, "id":2, "phase":"cleared", "uptime_ms":event["uptime_ms"]+500}), qos=1)
            await until(lambda: len(hub.events()) == 2)
            assert all(row["duration_ms"] == 500 for row in hub.events())
            start = await hub.command("start")
            assert start["camera"]["manual_live"] and start["control_transport"] == "mqtt"
            stop = await hub.command("stop")
            assert not stop["camera"]["manual_live"] and len(set(command_ids)) == 2
            # Broker restart must recover both clients and their subscriptions.
            before_restart = connection_count
            subprocess.run(["docker", "restart", name], check=True, capture_output=True)
            await until(lambda: hub.mqtt.connected and connection_count > before_restart, timeout=25)
            await until(hub.mqtt.ready, timeout=20)
            published = device.publish("doorbell/qa-device/events", json.dumps({**event, "id":3, "uptime_ms":uptime()}), qos=1)
            assert published.rc == mqtt.MQTT_ERR_SUCCESS
            await until(lambda: len(hub.events()) == 3)
            # A client with only the server CA and no client identity is refused.
            def unauthenticated():
                context = ssl.create_default_context(cafile=str(directory / "ca.crt"))
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
                        with context.wrap_socket(raw, server_hostname="127.0.0.1") as sock:
                            sock.sendall(b"\x10\x10\x00\x04MQTT\x04\x02\x00\x3c\x00\x04test")
                            response = sock.recv(4)
                            return not (len(response) == 4 and response[:2] == b"\x20\x02" and response[3] == 0)
                except (ssl.SSLError, ConnectionError, OSError):
                    return True
            assert await asyncio.to_thread(unauthenticated)
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            heartbeat = None
            # Abruptly close transport rather than a clean MQTT DISCONNECT: test LWT.
            await asyncio.to_thread(device.loop_stop)
            device.socket().shutdown(socket.SHUT_RDWR)
            device.socket().close()
            await until(lambda: not hub.mqtt.device_online)
            assert not hub.mqtt.status()["device_online"]
            print(json.dumps({"source":"real local Mosquitto broker; simulated device; isolated database",
                "mutual_tls":True, "unauthenticated_client_rejected":True, "qos1_event_receipt":True,
                "qos0_heartbeat":True, "state_over_1024_bytes_received":True,
                "trigger_clear_duration_pairing":True,
                "duplicate_event_deduplication":True, "mqtt_start_stop_acknowledged":True,
                "broker_restart_recovered":True, "last_will_offline":True, "stored_events":len(hub.events())}))
        except Exception:
            print(json.dumps({"diagnostic": hub.mqtt.status() if hub else {}, "stored_events":len(hub.events()) if hub else 0}), flush=True)
            print(subprocess.check_output(["docker", "logs", "--tail", "30", name], stderr=subprocess.STDOUT, text=True), flush=True)
            raise
        finally:
            if heartbeat:
                heartbeat.cancel()
            if device:
                device.disconnect()
                device.loop_stop()
            if hub:
                await hub.close()
            subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)


asyncio.run(main())
