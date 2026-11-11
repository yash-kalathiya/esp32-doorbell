"""Provision a project-local mutual-TLS broker; never print private key material."""
import argparse
from datetime import datetime, timedelta, timezone
import ipaddress
import json
from pathlib import Path
import re

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[1]


def write(path, data, private=False):
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    path.chmod(0o600 if private else 0o644)


def provision(directory, broker_ip, device_id, write_firmware=False, refresh_address=False):
    address = ipaddress.IPv4Address(broker_ip)
    if not address.is_private or address.is_unspecified or address.is_multicast:
        raise ValueError("Use the laptop's private LAN IPv4 address")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", device_id):
        raise ValueError("Invalid device ID")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    existing = (directory / "ca.key").exists()
    if existing and not refresh_address:
        raise ValueError("Existing MQTT identity found. Reuse it; do not overwrite a provisioned CA.")
    if existing and json.loads((directory / "gateway.json").read_text())["device_id"] != device_id:
        raise ValueError("Address refresh must use the existing device ID")
    broker_directory = directory / "broker"
    broker_directory.mkdir(exist_ok=True)
    broker_directory.chmod(0o700)
    (broker_directory / "data").mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    root_key = (serialization.load_pem_private_key((directory / "ca.key").read_bytes(), password=None)
                if existing else ec.generate_private_key(ec.SECP256R1()))
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Doorbell local CA")])
    ca = x509.load_pem_x509_certificate((directory / "ca.crt").read_bytes()) if existing else (x509.CertificateBuilder().subject_name(root_name).issuer_name(root_name)
        .public_key(root_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=730))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=None, decipher_only=None), critical=True)
        .sign(root_key, hashes.SHA256()))
    def save_identity(name, common_name, server=False):
        key = ec.generate_private_key(ec.SECP256R1())
        builder = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False))
        if server:
            builder = builder.add_extension(x509.SubjectAlternativeName([
                x509.DNSName("localhost"), x509.DNSName("doorbell-broker"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")), x509.IPAddress(address)]), critical=False)
        certificate = builder.sign(root_key, hashes.SHA256())
        write(directory / f"{name}.crt", certificate.public_bytes(serialization.Encoding.PEM))
        write(directory / f"{name}.key", key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), private=True)
    if not existing:
        write(directory / "ca.crt", ca.public_bytes(serialization.Encoding.PEM))
        write(directory / "ca.key", root_key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), private=True)
    save_identity("broker", "doorbell-broker", server=True)
    if not existing:
        save_identity("gateway", "doorbell-gateway")
        save_identity("device", device_id)
    for filename in ("ca.crt", "broker.crt", "broker.key"):
        write(broker_directory / filename, (directory / filename).read_bytes(), private=filename.endswith(".key"))
    write(broker_directory / "mosquitto.conf", """listener 8883
protocol mqtt
cafile /mosquitto/config/ca.crt
certfile /mosquitto/config/broker.crt
keyfile /mosquitto/config/broker.key
require_certificate true
use_identity_as_username true
allow_anonymous false
acl_file /mosquitto/config/acl
persistence true
persistence_location /mosquitto/config/data/
autosave_interval 30
max_queued_messages 1000
message_size_limit 4096
log_type error
log_type warning
log_type notice
log_dest stdout
""")
    write(broker_directory / "acl", f"""user doorbell-gateway
topic read doorbell/+/events
topic read doorbell/+/state
topic read doorbell/+/ack
topic write doorbell/+/commands
topic write doorbell/+/received

user {device_id}
topic write doorbell/{device_id}/events
topic write doorbell/{device_id}/state
topic write doorbell/{device_id}/ack
topic read doorbell/{device_id}/commands
topic read doorbell/{device_id}/received
""")
    config = {"host": "127.0.0.1", "port": 8883, "device_id": device_id,
              "ca": str(directory / "ca.crt"), "certificate": str(directory / "gateway.crt"),
              "key": str(directory / "gateway.key")}
    write(directory / "gateway.json", json.dumps(config, indent=2), private=True)
    if write_firmware:
        header = ROOT / "firmware/Doorbell/mqtt_credentials.h"
        parts = ["#pragma once", f'constexpr const char *DOORBELL_MQTT_HOST = "{address}";',
                 "constexpr int DOORBELL_MQTT_PORT = 8883;",
                 f'constexpr const char *DOORBELL_MQTT_DEVICE_ID = "{device_id}";',
                 f"constexpr long DOORBELL_MQTT_BUILD_EPOCH = {int(now.timestamp())}L;"]
        for symbol, filename in (("DOORBELL_MQTT_CA", "ca.crt"), ("DOORBELL_MQTT_CERT", "device.crt"), ("DOORBELL_MQTT_KEY", "device.key")):
            parts.append(f'constexpr const char *{symbol} = R"CERT({(directory / filename).read_text()})CERT";')
        write(header, "\n\n".join(parts) + "\n", private=True)
    return config


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker-ip", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--directory", type=Path, default=ROOT / ".local-data/mqtt")
    parser.add_argument("--write-firmware", action="store_true")
    parser.add_argument("--refresh-address", action="store_true", help="Reissue only the broker identity for a changed laptop IP; preserve the CA/client identities")
    args = parser.parse_args()
    provision(args.directory, args.broker_ip, args.device_id, args.write_firmware, args.refresh_address)
    print("Local CA, mutual-TLS identities, broker ACL and gateway configuration created. Private keys were not printed.")
