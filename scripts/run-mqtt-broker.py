"""Run only this project's broker, with its restricted TLS identity mount."""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
directory = ROOT / ".local-data/mqtt/broker"
if not (directory / "mosquitto.conf").exists():
    raise SystemExit("Run scripts/setup-local-mqtt.py first.")
name = "esp32-doorbell-mqtt"
existing = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", name], capture_output=True, text=True)
if existing.returncode == 0:
    if existing.stdout.strip() != "true":
        subprocess.run(["docker", "start", name], check=True)
else:
    subprocess.run(["docker", "run", "-d", "--name", name, "--restart", "unless-stopped",
        "--user", f"{os.getuid()}:{os.getgid()}", "--publish", "8883:8883",
        "--volume", f"{directory}:/mosquitto/config:rw,Z", "eclipse-mosquitto:2",
        "mosquitto", "-c", "/mosquitto/config/mosquitto.conf"], check=True, capture_output=True)
print("Project MQTT broker started on TLS port 8883; mutual client certificates are required.")
