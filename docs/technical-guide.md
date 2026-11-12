# Technical guide

[← Project overview](../README.md)

Detailed operating notes, protocol tables, verification commands and firmware
history for the ESP32 doorbell. For a first run, start with the
[README quick start](../README.md#quick-start).

**Current firmware: `reliable-1`.** Sections describing earlier builds and their
test counts are historical records. The latest measured results appear under
[Reliability update](#reliability-update-reliable-1).

- [Reliability and physical results](#reliability-update-reliable-1)
- [Security watch](#experimental-security-watch)
- [Automatic lighting](#automatic-camera-light-and-night-mode)
- [Face overlays and sensor durations](#live-face-labels-and-sensor-durations)
- [MQTT provisioning and delivery](#local-mqtt-setup)
- [Camera burst enrollment](#camera-burst-orientations-and-sample-count)
- [Photo uploads](#enroll-from-uploaded-photos)
- [Laptop API](#laptop-api-port-8000)
- [CSI pilot](#csi-pilot-with-the-existing-esp32-cam)
- [Firmware and wiring](#firmware-upload)
- [ESP32 API](#api-v1)
- [Development checks](#dashboard-checks)

Commands that include `/home/yash/Project/esp32-doorbell` describe the original
laptop installation; substitute your checkout path on another machine. Its
systemd user service is optional. Run `./scripts/run-dashboard.sh` from the
repository root for a foreground server.

---

## Laptop dashboard

### Reliability update: reliable-1

Upload the complete `firmware/Doorbell/` sketch for this update. Automatic video
now has a fixed **30-second window**; further sensor pulses do not extend it.
Manual live viewing remains available. Sound pulses are one incident until
**two seconds of quiet**, with a minimum three seconds between accepted starts.
The first sound incident also requires quiet after startup. A PIR staying HIGH
for 60 seconds after its accepted start, or a sound incident lasting 60 seconds,
is reported as sensor trouble. Raw inputs and ongoing motion remain visible;
the firmware does not invent a clear event. A faulty PIR cannot keep automatic
video running or prevent a healthy sound input from eventually rearming.

The sound input uses a direct IRAM GPIO handler instead of Arduino's
flash-resident interrupt trampoline. The pin interrupt is masked for at least
200 ms after each interrupt; the sensor task samples it every 20 ms meanwhile.
This bounds interrupt work, but raw sound period counts/durations are sampled
during that holdoff and can miss short changes. Sound incident duration includes
the quiet rearm interval; it is not the duration or loudness of recorded audio.
The input is a digital threshold detector and no audio is recorded.

The camera uses one PSRAM frame buffer, initializes at VGA and starts streaming
at QVGA. Live transmission is capped at 10 FPS. **CSI and camera requests run
separately**: stop live video before starting CSI. During CSI the gateway pauses
automatic viewing, background face snapshots and automatic lighting work.
Manual Start live first requires confirmation that CSI stopped. Firmware also
excludes overlapping CSI and snapshot/stream requests. CSI remains off after
boot, Wi-Fi loss or its lease/heap stop; restarting it needs a new calibration.
Its capture target is 10 Hz; the receiver still requires at least 5 Hz.

Gateway stream retries back off up to 15 seconds and pause while the camera is
offline, idle or capturing CSI. A new boot invalidates cached photos/overlays,
while profiles, settings and event history remain stored on the laptop.
Build verification and simulated interruptions are separate from the physical
checks below. Sustained hardware stability and CSI classification still require
measurement before relying on intrusion-alert fusion.

Measured on the uploaded `reliable-1`, November 12, 2026:

- A 121-second video/MQTT observation received 1,109 JPEG frames in one boot.
  MQTT stayed online in every sample, Wi-Fi/MQTT disconnect counts stayed zero,
  and the stream-error counter stayed at its starting value of 1. Sampled free
  internal heap stayed at or above 9,152 bytes; since-boot low-water was 4,492.
  This is a short test, not long-duration qualification.
- With the project broker stopped, 18 pending events were retained through a
  user-confirmed power loss. After power and broker recovery, all 18 original
  event IDs were received through MQTT, each stored once, and the pending queue
  returned to zero. The historical cumulative lost-event counter did not rise.
- Restarting the laptop dashboard restored its saved settings, four profiles,
  nine enrollment samples and event history, and reconnected to the same device
  boot. Counts describe the test state, not bundled demonstration identities.
- PIR OUT disconnected from IO13 produced LOW. Reconnecting it produced HIGH;
  reseating/power cycling did not resolve prolonged HIGH. Its hardware fault
  remains open, and is reported without falsely clearing motion. The user has
  no multimeter, so the exact sensor/wiring/supply cause has not been measured.
- A separate 180-second CSI-only observation received 1,248 packets at sampled
  rates of 6.2–7.6 Hz, with one receiver sequence gap and no rejected packets.
  MQTT stayed online in one boot, the stream-error counter stayed zero, and
  sampled free internal heap stayed between 23,444 and 24,404 bytes. Capture
  never hit its heap guard. This validates short acquisition stability, not
  human classification or continuous protection.

The physical router-outage check was deferred because the user could not
interrupt the shared router. Power, broker replay and laptop-service recovery
were exercised; ESP32 Wi-Fi reassociation during an AP outage remains untested.
Two newer empty-room recordings each contain 30 valid windows. The following
two nominal walking recordings were later confirmed to include movement of the
ESP32 through router distances of 1–2.5 m while people also moved. They are
preserved but **excluded from human-motion comparison**, because receiver and
human movement cannot be separated. Furniture and screens present in the room
are background context, not a completed moving-fan false-positive test. A new
fixed-device, labelled walking trial and non-human-disturbance testing remain.

Reliability-only verification: 80 automated tests plus explicit native sensor harnesses
passed. Isolated CSI browser checks, real-model enrollment browser checks and a
real TLS broker with a simulated device passed. These do not substitute for the
deferred router-outage check or controlled human/disturbance validation.

### Experimental security watch

Under **Wi-Fi → Security watch**, start CSI with video stopped, calibrate an
empty room, then select **Arm away**. A 15-second exit delay precedes monitoring.
Five consecutive fresh measurement windows above the calibrated threshold save
one **possible-intrusion** incident. Three quiet windows rearm the incident rule;
a 60-second cooldown limits repeated incidents. Missing, low-rate or
uncalibrated data is **Unavailable**, never evidence that the room is empty.
The ten-minute CSI lease and memory guard still apply: this is a bounded
experimental watch, not continuous validated home protection.

Each incident attaches up to three camera observations from the previous two
minutes, if any, plus device/session and sensor-fault context. Camera observations
are historical context, not proof of who caused CSI movement. Known faces do not
automatically authorize later anonymous motion. No person count, room track or
directional entrance/exit is inferred. A faulty PIR is not used to trigger the
security rule. No new ESP32 firmware upload is needed for these laptop rules.

Final software checks: **89 automated tests passed**, plus the isolated browser
check for arm/disarm, incident acknowledgement, missing data and mobile layout.
The initial rule was replayed offline against the saved mixed-motion recordings,
but those confounded trials do not validate human or intrusion detection. The
[demo video](demo/demo_video.mp4) now shows an armed watch, a new incident
and acknowledgement. This demonstrates the workflow, not a controlled intrusion
test. Leave this feature experimental until fixed-device trials and
false-positive checks support it.

Incidents appear locally in the dashboard and can be acknowledged. The latest
100 are kept in the laptop database, with the latest 20 displayed. They survive
server restarts, but the watch restarts **disarmed**. Starting video stops CSI,
so an armed watch becomes unavailable while video is in use. This prototype
does not send remote notifications, and its laptop server must remain running.

Rule tests use synthetic measurements and isolated galleries/brokers. Browser
checks cover arming, incidents, acknowledgements and missing-data display. These
tests establish software behavior, not human-detection accuracy. Recordings made
while moving the camera/router or changing the room layout must not be used as
human-motion validation. Recalibrate after moving either radio endpoint. New
trial comparisons require matching capture session, calibration, boot, radio
geometry and video mode so different placements are not silently pooled.

API: GET `/api/security`, POST `/api/security/arm`, `/api/security/disarm`,
and `/api/security/incidents/{id}/acknowledge`. Mutating endpoints use the
dashboard's existing same-origin checks.

Primary implementation references: [Espressif GPIO interrupt service](https://github.com/espressif/esp-idf/blob/v5.5.2/components/esp_driver_gpio/src/gpio.c),
[ESP32 PSRAM restrictions](https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32/api-guides/external-ram.html),
and the installed Arduino 3.3.12 `esp32-hal-gpio.c` trampoline. A captured
`csi-pilot-1` cache-disabled GPIO-dispatch panic matched its saved ELF hash;
the direct IRAM handler removes the observed flash-handler hazard. It does not
establish a cause for every previous reset.

The dashboard runs on your laptop and works in a phone browser on the same
Wi-Fi. It relays live MJPEG video, offers Start/Stop and downloadable snapshots,
shows camera/sensor status, saves received motion/sound events locally, and
recognises enrolled faces using local YuNet + SFace models on the laptop.

1. Power the ESP32-CAM normally, with the IO0–GND upload jumper removed.
2. Connect the ESP32-CAM, laptop, and phone to the same Wi-Fi. Keep the laptop
   awake while using the dashboard.
3. Open `http://localhost:8000` on the laptop. Select the settings icon and enter
   the camera's current IP address (from Serial Monitor). The default is the
   last known home address, `192.168.12.140`.
4. Select **Start live**. Close any old direct ESP32 video stream first: the
   laptop maintains one connection to the camera and shares it with its viewers.
5. Select **Open on your phone** at the bottom of the dashboard. It shows the
   laptop's current LAN URL, such as `http://LAPTOP_WIFI_IP:8000`.

Both the laptop and camera IP addresses may change when Wi-Fi changes. The
phone opens the **laptop's** address; camera settings use the **ESP32's** address.
Guest/campus networks may prevent devices from reaching each other. The app
reports unavailable devices without displaying sample video or invented events.

The running user service lasts until stopped or the laptop reboots:

```bash
systemctl --user status esp32-doorbell-dashboard
systemctl --user stop esp32-doorbell-dashboard
```

To start it again:

```bash
systemd-run --user --collect --unit=esp32-doorbell-dashboard \
  --property=Restart=on-failure \
  --working-directory=/home/yash/Project/esp32-doorbell \
  /home/yash/Project/esp32-doorbell/scripts/run-dashboard.sh
```

For foreground development, stop the service first, then:

```bash
cd /home/yash/Project/esp32-doorbell
# Only needed when setting up on a new laptop:
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/download-face-models.py
./scripts/run-dashboard.sh
```

The server listens on `0.0.0.0:8000`. `PORT=8001 ./scripts/run-dashboard.sh`
selects a different port. Logs: `journalctl --user -u esp32-doorbell-dashboard`.

**Behavior and limits:** Start/Stop control the camera's shared manual-live
state. Stop also pauses viewing in the current browser; it does not cancel a
sensor's activity window. The activity toggle automatically displays available
video while this page is open. It does not record video. Face monitoring is a
separate server function with its own toggle (see below).
The sound module supplies digital threshold events, not microphone audio.

### Automatic camera light and Night mode

The dashboard follows the Apple Home app layout: a laptop sidebar, phone bottom
navigation, compact status tiles and a prominent camera view. People and unknown
face review stay visible; **Sensor details** and **Connection details** expand
on demand. Touch controls and accessibility fallbacks remain.

The camera light is **automatic**, using **50% PWM only when the image is dark**.
It remains off at boot. The laptop estimates image brightness every three seconds
from the shared live frame or an idle snapshot shared with face monitoring. No
second camera stream or ESP32 image buffer is added. Three consecutive dark
samples enable the lamp; three bright samples disable it, with separate thresholds
and a 30-second minimum hold between changes. Illumination remains gated to
streaming and snapshots by the firmware.

This is an image-based estimate, not an ambient lux reading. Camera automatic
exposure, bright backgrounds and the lamp itself affect it. Defaults are mean
8-bit grayscale ≤55 to turn on and ≥115 to turn off; calibrate these against the
actual installation. The laptop server must be running for automatic decisions.
If face monitoring is paused, lighting can still request idle snapshots.

**Night mode** is a separate camera profile: automatic gain/exposure remain on,
with brightness and exposure target raised to +1. Turning it off restores both
to 0. It does not force the lamp on. The preference is saved on the laptop and,
with the new firmware, in the ESP32's NVS. Camera controls are reapplied after
reconnection/reboot and their confirmation is shown beside the button. Older
firmware can use the existing `/control` endpoint until the new sketch is uploaded.

Open `firmware/Doorbell/Doorbell.ino` with **all files in that sketch directory**
and upload once. The current build identifies itself as `reliable-1`; it includes
the memory optimizations, incident policy, sensor timing and CSI/video isolation.
Compilation used **1,302,301 bytes of flash and 76,312 bytes of static RAM**.
Software checks and physical reliability results are recorded separately below.

### Live face labels and sensor durations

Live video now displays face boxes with an enrolled name, **Unknown**, or a
quality hint (**Move closer** / **Improve lighting**). Detection and matching
run on the laptop using the already-received video frames; no face processing
or extra frame buffer is added to the ESP32. Boxes scale with the displayed
image, including phone layouts and letterboxing. Checks run about once a second
while viewing, with stored observations limited to about every three seconds.
These are recent analysis results rather than per-frame tracking. Lower-confidence
candidates get a **Face candidate** box; they are not named or enrolled.
Boxes expire within five seconds of their source frame and clear when video
stalls, the device changes/reboots, or face monitoring is paused. Existing
recognition and enrollment thresholds remain in force; a quality hint does not
enroll or identify a low-quality face. Refresh the dashboard after updating it.

The Activity panel shows each pin's **HIGH/LOW state**, how long that level has
been observed, the last completed active duration, and accepted alert count for
the current boot. PIR is active **HIGH**; the current sound module is active
**LOW**, so a sound HIGH period is idle time. A continuously HIGH PIR produces
one motion activation until it clears and rises again. Sound pulses are grouped into incidents until two seconds of quiet;
short raw pin periods are not separate accepted alerts.

Upload the current `reliable-1` firmware for timing support; it includes the
`mqtt-mem-1` PSRAM optimizations. The laptop overlay works without this reflash.
Status includes `sensor_timing_version: 1` and `sensor_levels` with current
level duration, HIGH/LOW period counts, completed durations, current active
event IDs and accepted trigger counts. Raw period counts include the first
observed level at startup. PIR is sampled about every 20 ms, with the existing
100-ms stable-state check and 60-second warm-up. Sound edges are latched in
the GPIO ISR, with no networking, allocation or logging there. The interrupt
is masked for 200 ms and the pin is sampled every 20 ms during that holdoff;
short raw periods can therefore be missed. Sub-millisecond recorded periods
display as **<1 ms**; this is not a promise to capture every such pulse.

New events retain `type: motion|sound` and add `phase: triggered|cleared`.
The gateway pairs them by camera, device, boot and sensor using device uptime,
including clock rollover, duplicate transport deliveries and out-of-order
arrival. The UI shows one card per matched activation: **Active for … · ongoing**
or **Was active for … · cleared**. A new activation creates another card.
An unmatched clear remains visible with duration unavailable. Older events
without timing metadata are preserved and explicitly show that duration was
not recorded. Durations stop advancing when status becomes stale; boot IDs
prevent pairing events across a restart.

The NVS outbox record layout and 24-record capacity are preserved; the new
reader accepts the prior trigger-only records. Completed-period records also
consume outbox slots, so the visible loss counter still matters during long
offline periods. Broker configuration is unchanged. Validation includes 50
Python/native checks, C++ level/lifecycle/outbox tests, an isolated real-model
browser test, and a real local TLS broker test with a simulated device. The
original timing firmware compiled for AI Thinker ESP32-CAM. Later physical
observations on its `reliable-1` successor are listed under
[Reliability update](#reliability-update-reliable-1).

Fresh MQTT telemetry and events are the primary path after the MQTT firmware
is uploaded. Without a connected MQTT device, the server polls the existing
HTTP firmware every two seconds as a fallback; that path needs no reflash.
Received events are deduplicated using
camera address, device ID, boot ID, and event ID. The newest 2,000 events remain
in `.local-data/events.sqlite3` across server restarts; the newest 100 for the
selected camera appear in the feed. Feed times indicate **first received by
the laptop**, not the exact detection time. Camera uptime is shown separately.
Settings live in `.local-data/settings.json`. These files contain no Wi-Fi
credentials. The app is intended for a trusted LAN and has no user login.

Source: `server/app.py` (FastAPI relay), `web/` (responsive dashboard),
`scripts/run-dashboard.sh` (launcher).

### Local MQTT setup

The gateway uses Mosquitto on port **8883**, MQTT 3.1.1, QoS 1 and mutual TLS.
Each device has a client certificate and topic ACL; anonymous connections are
refused. The broker container mounts only its own identity/configuration, not
the CA signing key or the device/gateway private keys.

First-time setup (use the actual laptop Wi-Fi IP and the ESP32's `device_id`):

```bash
.venv/bin/python -m pip install -r requirements.txt
docker pull eclipse-mosquitto:2
.venv/bin/python scripts/setup-local-mqtt.py \
  --broker-ip LAPTOP_WIFI_IP --device-id ESP32_DEVICE_ID --write-firmware
.venv/bin/python scripts/run-mqtt-broker.py
```

Restart the dashboard after provisioning. For a foreground server, press Ctrl+C
in its terminal and rerun `./scripts/run-dashboard.sh`. On the original laptop
with the systemd user service, use
`systemctl --user restart esp32-doorbell-dashboard` instead.

The setup creates `.local-data/mqtt/` and the Git-excluded
`firmware/Doorbell/mqtt_credentials.h`. Compile and upload the sketch **once**
to enable MQTT on the ESP32; a server restart cannot update board firmware.
The topic identity must match the real board ID. The setup fails safely rather
than silently replacing an existing CA.

When changing from eduroam to the home network or hotspot, refresh the laptop
address before compiling/uploading:

```bash
.venv/bin/python scripts/setup-local-mqtt.py \
  --broker-ip NEW_LAPTOP_WIFI_IP --device-id ESP32_DEVICE_ID \
  --refresh-address --write-firmware
docker restart esp32-doorbell-mqtt
```

This preserves the CA/client identities, updates the broker certificate and the
firmware's default broker address, and does not print private keys. Upload the
rebuilt firmware when its broker address changes. Broker state and queued
messages are stored under `.local-data/mqtt/broker/data/`.

| Topic suffix under `doorbell/DEVICE_ID/` | Direction | Behavior |
| --- | --- | --- |
| `events` | ESP32 → gateway | QoS 1, not retained; deduplicated and committed to SQLite |
| `received` | Gateway → ESP32 | Application receipt after database commit |
| `state` | ESP32 → gateway | Retained QoS 0 heartbeat every 5 seconds; QoS 1 offline Last Will |
| `commands` | Gateway → ESP32 | Non-retained start/stop, unique command ID, boot binding and short expiry |
| `ack` | ESP32 → gateway | Application acknowledgement and resulting state |

The ESP32 keeps up to 24 pending events in a checksum-validated NVS outbox until
the gateway confirms storage. It retries unconfirmed events. Overflow drops the
oldest event and increments a visible loss counter. NVS-write failure is exposed
by `mqtt_outbox_persistent`; RAM delivery can continue, but power-loss retention
then cannot be assumed. NVS flash endurance under sustained triggering has not
been evaluated. Previously unconfirmed events retain their original boot IDs
after a reboot.

The broker's PUBACK is not treated as proof that the application saved an event.
Duplicate transport deliveries and duplicate HTTP fallback observations create
one event row, preserving its first receipt time. The `source` field shows
`mqtt`, `http`, or `mqtt+http`.

Commands are not confirmed to the UI until an application ACK arrives. Old-boot,
expired, retained or malformed commands are rejected; recent command IDs are
cached against duplicate execution. Historical retained-online state is not
proof of liveness: a fresh heartbeat is required, and state expires after
20 seconds. A Last Will or stale heartbeat moves the gateway to HTTP fallback.

The dashboard's health strip shows broker connection, device MQTT status,
selected control path and SSE live updates. Browsers receive changed status,
activity and face data over `/api/updates`; HTTP polling is a reconnect fallback.

This is the **local project milestone**. The broker transport is encrypted and
certificate-authenticated. The existing camera HTTP API and dashboard HTTP
listener remain LAN prototype interfaces without user login. Remote access,
browser HTTPS deployment and WebRTC remain outside the local milestone.
The separate CSI pilot below has completed initial physical trials; reliability,
coverage and human-detection accuracy remain unvalidated.

### Connection diagnostics firmware

The first diagnostic build identified itself as `mqtt-diag-1`; the memory
optimization build reports `"firmware_version":"mqtt-mem-1"` in the ESP32's
`/api/status` response and MQTT state. Upload the complete
`firmware/Doorbell/` sketch, including its new `doorbell_diagnostics.cpp` and
`.h` tabs. The current `reliable-1` build includes those optimizations, sensor timing,
Night mode and optional CSI capture. Neither build needs a broker configuration update.

After uploading, remove the IO0–GND jumper with power disconnected, restart the
board, and open `http://ESP32_IP/api/status`. The laptop's `/api/status` also
includes these fields under `camera` when it receives fresh device status.

| Field under `diagnostics` | What it helps distinguish |
| --- | --- |
| `reset_name`, `reset_reason` | Power-on, watchdog, panic or brownout reset reported by the chip |
| `memory` | Current/minimum free internal heap, largest free internal block and free PSRAM, in bytes |
| `wifi` | IP connection/disconnect counts and the last Wi-Fi disconnect reason |
| `mqtt` | Connection attempts, successful connections, disconnects and errors |
| `mqtt.last_error` | Error type, ESP/TLS codes, certificate flags, socket errno, broker code and internal heap sampled at the error |
| `camera` | Capture failures/durations, captures currently in progress, completed stream frames and stream errors |

The last MQTT error stays available after reconnecting; `last_error` is `null`
until an error occurs. Counts and error details reset on reboot and are scoped
to the response's `boot_id`. Event times are milliseconds since boot; `unix_time`
shows the board's wall clock for investigating certificate validity. Numeric
Wi-Fi reasons and TLS/certificate codes must be interpreted for the installed
ESP32 SDK, rather than treated as proof of a power or memory fault.

Serial Monitor at **115200 baud** also prints concise `[diag]` connection/error
lines. MQTT now starts after Wi-Fi obtains an IP, avoiding the observed startup
transport attempts before Wi-Fi was ready. The runtime MQTT timeout and TLS
certificate verification are unchanged. This build adds visibility; a fix for
the intermittent runtime disconnects or camera stalls has not been verified.

The native diagnostic serializer test uses fake hardware inputs with the real
cJSON library. It verifies retained error details across reconnect, capture
tracking, and message size, without claiming a physical-board result:

```bash
.venv/bin/python -m pytest -q tests/test_firmware_diagnostics.py
```

It requires `g++`, the system `libcjson` runtime and the ESP32 SDK cJSON header;
it skips if they are missing. `ESP32_JSON_INCLUDE` can select a different SDK
header directory. The Arduino firmware compile remains the target-board check.

### Internal memory optimization (`mqtt-mem-1`)

Physical testing of `mqtt-diag-1` caught internal free heap dropping to a
48-byte minimum while video and TLS ran together. TLS reported SSL/MPI allocation
failures, despite roughly 3.4 MB of unused PSRAM and zero recorded Wi-Fi drops.
The installed Arduino SDK builds mbedTLS with internal-memory allocation.

This build reserves more internal heap for networking by changing application
allocations:

- Install cJSON allocation hooks once, before camera/network tasks start, so
  JSON trees, keys, strings and serialized output use PSRAM. Boards without
  PSRAM retain an internal-memory path. On a PSRAM board, failed PSRAM JSON
  allocations are reported rather than silently consuming TLS's internal heap.
- Move the MQTT inbox's **8,224-byte item storage** to PSRAM, preserving all 16
  slots and the 511-byte incoming-message bound. Queue bookkeeping and task
  stacks stay internal; the queue is accessed only from tasks, never an ISR.
- Reduce MQTT input/output buffers from 2,048 to 1,024 bytes each, removing
  **2,048 bytes** of buffer demand. ESP-MQTT supports larger outgoing payloads
  through fragmentation. Application JSON remains bounded below 4 KiB for MQTT;
  HTTP event-history JSON keeps its existing variable size.
- Send replaceable retained heartbeats at QoS 0, directly from the worker,
  instead of accumulating stale state copies in the QoS 1 retry outbox. A lost
  heartbeat is replaced by the next one. Events, application receipts, commands,
  command ACKs and the offline Last Will retain their reliable delivery paths.

TLS verification, certificates, the durable sensor-event outbox, camera frame
size and task-stack sizes are preserved. JSON serialization uses a bounded
4-KiB PSRAM output buffer to avoid repeated growth/copy allocations. This trades
available PSRAM for internal-heap headroom; it does not claim every message uses
less total RAM. The changed buffers/queue account for about 10 KiB less dynamic
internal storage demand, before JSON and heartbeat-copy savings.

**Verification before uploading:**

| Check | Diagnostic build | Optimized build |
| --- | --- | --- |
| Arduino static globals | 76,224 bytes | 76,344 bytes |
| Arduino program storage | 1,292,897 bytes | 1,294,309 bytes |
| MQTT I/O buffer settings | 4,096 bytes combined | 2,048 bytes combined |
| MQTT inbox payload allocation | 8,224 internal bytes | 8,224 PSRAM bytes |
| Controlled native JSON internal allocation peak | 31,041 host bytes | 0 host bytes |
| Same native workload, combined allocation peak | 31,041 host bytes | 31,041 host bytes |

The 120-byte increase in static globals includes internal queue bookkeeping
previously allocated dynamically. Arduino's static-global figure does not
measure runtime heap consumption. The native comparison uses the real cJSON
library and firmware allocation hooks, simulated heap capabilities, three
concurrent 70-field JSON documents, and the same 4-KiB output buffer in both
passes. Host pointer sizes and allocator overhead differ from the ESP32; these
counts verify routing, not physical free heap. A further 2,000 threaded
create/serialize/free cycles left no live tracked allocations. Tests also cover
no-PSRAM operation, allocation failure and oversized-message rejection.

All 46 Python/native checks passed. An isolated real Mosquitto/mutual-TLS test
with a simulated device passed QoS 0 heartbeat delivery (including a state
larger than 1,024 bytes), QoS 1 event deduplication/receipts, command ACKs, broker
restart and offline Last Will. It does not test ESP32 transport fragmentation
or physical TLS/video stability. The firmware compiles for AI Thinker ESP32-CAM.

After uploading, check `firmware_version`, `diagnostics.memory.json_psram`,
`mqtt_inbox_psram`, and `mqtt_inbox_storage_bytes`. Compare the internal heap
minimum/largest block and disconnect/error counters during simultaneous video
and MQTT over several minutes. A new boot resets the heap minimum and counters.
**The optimized build's actual heap margin and disconnect rate still require
this physical upload/test.** No broker configuration change is needed.

Implementation references: [Espressif heap capabilities](https://docs.espressif.com/projects/esp-idf/en/release-v5.5/esp32/api-reference/system/mem_alloc.html),
[PSRAM restrictions](https://docs.espressif.com/projects/esp-idf/en/release-v5.5/esp32/api-guides/external-ram.html),
[ESP-MQTT publication, buffering and fragmentation](https://docs.espressif.com/projects/esp-idf/en/release-v5.5/esp32/api-reference/protocols/mqtt.html),
and [cJSON allocation hooks, preallocated printing and thread safety](https://github.com/DaveGamble/cJSON).

### Hardware validation

The MQTT firmware has been uploaded to the physical ESP32-CAM. Live checks on
November 11, 2026 confirmed an authenticated MQTT connection, a received motion
event, a cleared pending-event queue, and the video relay receiving frames.
After the `mqtt-mem-1` upload, a 60-second physical observation recorded zero
MQTT/Wi-Fi disconnects and zero camera errors. A 40-second video read received
1,013 frames (about 25 FPS), and both motion and sound events arrived via MQTT.
The recorded minimum internal heap was 2,944 bytes, versus the earlier 48-byte
minimum. This was a short stability check; restart recovery, longer operation,
recognition accuracy and physical `night-light-1` duration checks remain open.
Software and simulated-device checks do not establish those hardware results.

1. For a new setup, join the camera and laptop to the same home Wi-Fi/hotspot, update the broker
   address as above, then upload the sketch with the working USB wiring. Remove
   the IO0–GND upload jumper for normal operation.
2. Check that the dashboard shows **MQTT broker: Connected**, **Device MQTT:
   Online**, and **Dashboard updates: Live · SSE**. Device discovery should
   update the camera IP when its authenticated heartbeat arrives.
3. Wait for PIR warm-up. Confirm the PIR goes clear, then trigger fresh motion.
   Check one activity entry arrives with MQTT as its source. Repeated transport
   deliveries must not create duplicate entries. Validate sound polarity and
   threshold separately; an always-active input is not a successful sound test.
4. Start and stop live view. Confirm MQTT control is selected and the command is
   acknowledged; verify live video and a snapshot independently of telemetry.
5. Stop the project broker, trigger a fresh event and inspect
   `http://ESP_IP/api/status`: pending events should increase. Restart the broker;
   confirm the event arrives and the device pending count returns to zero.
6. Separately test power loss with an unconfirmed event, checking its persistence
   flag first. After reboot/reconnection, verify that event arrives once with
   its original boot ID. Measure actual video stability with TLS active.
7. Test recognition with enrolled and unenrolled people under the intended
   lighting, and record observed false matches/misses. Record a backup video of
   the actual working hardware demonstration after these checks pass.

Do not present the simulated-device integration test as physical sensor results.

### Camera burst, orientations and sample count

Select **People → ESP32 camera burst**, enter a name and select **Capture 8 photos**.
The camera source is the ESP32 only. Frames are about one second apart; the laptop
reuses an existing live frame when available and otherwise requests a snapshot.
No extra stream or ESP32 firmware change is required. Review the eight previews,
uncheck unwanted photos, then save. Photos stay in browser memory until saved;
closing the dialog cancels capture and discards its temporary images.

Naming an unknown face defaults to this burst workflow; its reviewed face is the
identity reference and is saved together with accepted burst samples. Uncheck
**Add an 8-photo ESP32 camera burst** to save just the existing capture. A burst without
a reference uses the first usable face (or existing samples for that name).
Keep only the intended person in view: these similarity checks are not proof of
identity. The server rejects multi-face, small, blurry or weak detections and
views below 0.65 similarity to the reference. Burst embeddings at or above 0.995
similarity to an existing/accepted view are skipped as near-duplicates. Thresholds
are project policy and need testing with actual people and lighting.

**Sample count:** start with **5–10 distinct, clear real photos**; the burst takes
8. This is a practical starting point, not a published SFace optimum. OpenCV's
SFace interface extracts an embedding per aligned face; it does not prescribe a
fixed gallery size or ideal enrollment count. There is **no per-person sample-count cap**:
accepted enrollment samples remain until the profile is removed. The dashboard
accepts larger selections and sends sequential batches of at most 10 photos and
40 MB, with an 8 MB per-photo limit. The gallery still supports 100 people.
More clear viewpoints and lighting variation are useful to test; many copies of
the same image do not add a new physical view of a face.

Detection now checks input sizes **320, 640 and 960**. If no usable face is found,
it also tries **90°, 180° and 270°** image rotations, aligns the detected face for
SFace, then maps boxes back to the original display coordinates. This applies to
both enrollment and subsequent recognition. Rotated/scaled scans are temporary
analysis, not dozens of duplicate stored samples, and cannot synthesize a side
profile. Existing identity and quality gates remain unchanged. The extra scans
run on the laptop and cost additional CPU time when upright detection fails.

Real-model regression checks on the public OpenCV sample passed all four image
orientations and three source-image scales, retaining the original identity
match and valid overlay coordinates. These checks do not establish accuracy on
unseen people or the physical ESP32 installation.

Sources: [OpenCV face detection/recognition tutorial](https://docs.opencv.org/4.12.0/d0/dd4/tutorial_dnn_face.html),
[official SFace implementation](https://github.com/opencv/opencv_zoo/blob/main/models/face_recognition_sface/sface.py).

### Name an unknown face

1. Connect the camera and laptop to the same Wi-Fi, then open **People** in the
   dashboard. **Monitor faces** is enabled by default.
2. Face monitoring checks about once a second during live viewing and about
   every three seconds otherwise, including with no browser open. It reuses current live-stream frames;
   when video is idle, it requests `/api/snapshot` from the ESP32 instead.
3. A usable unrecognised face appears as a photo under **Unknown faces**.
   Review that photo, type the person's name and select **Save person**. The
   default burst option opens capture and review; save the reviewed burst to
   finish enrollment, or uncheck it to enroll only the existing face.
4. The laptop saves its SFace embedding as an enrolled sample. Later camera
   images are compared against saved samples; no model training is required.
5. Enter an existing name on another unknown capture to add a sample to that
   profile. Name matching ignores case. More clear, varied samples can help.

Saved people and recent recognised captures appear below the unknown captures.
Background refresh preserves partially typed names. **Remove** deletes a
profile, its saved samples, and its associated face history. Future sightings
can then appear as unknown again.

### Enroll from uploaded photos

Select **Upload photos** in People, enter a name, choose photos and select
**Add photos to recognition**. **Add photos** on a saved profile opens the same
form with its name filled in. An existing name adds samples to that profile.
Enrollment works with the ESP32 offline and with background monitoring paused.

Select any number of JPEG, PNG, WebP, HEIC/HEIF or AVIF images, up to 8 MB each.
The page sends sequential requests of at most 10 photos and 40 MB, shows each
photo's result and preserves earlier additions if a later batch fails.
Each photo must contain exactly one detected, usable face of the
intended person. The server checks the decoded format, fixes EXIF orientation,
strips metadata, and rejects images over 24 megapixels. Small/blurry faces,
group photos, corrupt files and unsupported formats receive individual reasons.
Usable photos are added even if other files fail; no empty profile is created
when all photos fail. The error banner includes the actual rejection reason.
JPEG-based MPO files, which can also use a `.jpeg` extension, are read using
their primary image. Other unsupported formats report their detected format.

Only face crops and embeddings are retained. Original uploads are closed after
processing and are not retained as enrollment files or logged. Uploaded photos
are not recorded as camera sightings. Profiles retain all accepted samples until
removed; adding more does not discard older views. Larger galleries use more
laptop storage and recognition time.


### Laptop API (port 8000)

These are the laptop endpoints; the firmware endpoints below run on the camera.

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/status` | Connection state, latest camera status, video relay status |
| GET | `/api/updates` | Server-sent `status`, `activity`, `faces` updates; heartbeat and reconnect support |
| GET | `/api/events` | Up to 100 saved events for the selected camera |
| POST | `/api/events` | Accept and deduplicate an optional firmware callback |
| GET | `/api/settings` | Configured camera base URL |
| PUT | `/api/settings` | Save `{"camera_url":"http://192.168.12.140"}` locally |
| GET | `/api/network` | Current laptop LAN URL for the phone |
| POST | `/api/live/start` | Request manual live view from the camera |
| POST | `/api/live/stop` | Clear the camera's manual-live request |
| GET | `/api/stream` | MJPEG shared from one camera stream |
| GET | `/api/snapshot` | Download a JPEG from the camera |
| GET | `/api/enrollment/frame` | Fresh ESP32 burst frame, reusing live video when available |
| GET | `/api/faces` | Model/monitoring state, unknown captures, people, recent matches |
| POST | `/api/faces/{id}/enroll` | Save `{"name":"Person name"}` for a reviewed capture |
| GET | `/api/faces/{id}/image` | Captured face thumbnail |
| GET | `/api/people/{id}/image` | Profile thumbnail |
| DELETE | `/api/people/{id}` | Remove a profile, samples and associated face captures |
| POST | `/api/people/upload` | Multipart `name` + repeated `files` fields; enroll usable photo samples |

`POST /api/people/upload` also accepts `burst=true` and an optional `capture_id`
reference for reviewed burst enrollment. Per-frame results distinguish added,
skipped duplicates and rejected photos. `/api/faces` includes the enrollment
limits and orientation/scale settings.

`PUT /api/settings` also accepts `"face_monitoring": false` to pause automatic
face checks and `"night_mode": true` for the low-light camera profile. GET current
settings and preserve other fields when updating a preference. `/api/status`
includes `camera_controls` with image brightness, requested lamp percentage,
Night mode, and whether settings were confirmed by the camera. The camera also
accepts POST `/api/night/start`, `/api/night/stop`, `/api/light/start` (50%) and
`/api/light/stop`; these are local HTTP camera controls, while existing MQTT
events and live commands are unchanged.

### Dashboard checks

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests/test_dashboard.py tests/test_faces.py tests/test_mqtt.py
# With the dashboard running and Google Chrome installed:
.venv/bin/python scripts/check-dashboard.py
```

Protocol tests use a synthetic camera to verify cursor resets, deduplication,
history persistence, offline status, one upstream for multiple viewers,
connection cleanup, controls and snapshots. Browser checks exercise the running
UI at desktop/mobile widths and use clearly synthetic responses for live-video
controls. They do not establish real-camera or physical-phone streaming.

Face tests cover enrollment, subsequent matching, unknown/ambiguous results,
profile persistence/deletion, API validation, camera scoping, background
snapshot checks and reuse of the existing stream. The isolated real-model
browser test uses an OpenCV sample image and a temporary gallery:

```bash
mkdir -p artifacts
curl -fsSL -o artifacts/face-model-test.jpg \
  https://raw.githubusercontent.com/opencv/opencv/master/samples/data/lena.jpg
.venv/bin/python scripts/check-faces-ui.py
```

That check verifies enrollment, overlays, ESP32 burst review and real-model
photo upload in an isolated gallery. It does not measure household accuracy or
physical CSI sensing. No test identities or sample images are inserted into the
running gallery.

MQTT integration testing uses a real Mosquitto container, fresh test identities,
an isolated database and a simulated device. It tests TLS client authentication,
QoS 1 delivery, application receipts, deduplication, start/stop ACKs, broker
restart recovery and Last Will offline detection:

```bash
.venv/bin/python scripts/check-mqtt.py
g++ -std=c++17 -Wall -Wextra -Werror tests/doorbell_delivery_test.cpp -o /tmp/doorbell-delivery-test
/tmp/doorbell-delivery-test
```

The integration script removes only its own temporary broker. It never sends
test events to the running household broker. The host C++ tests cover bounded
outbox overflow, reboot restoration/checksum validation, ACK ordering, duplicate
commands, stale boot IDs, expiry and uptime rollover. These do not validate
physical NVS behavior, TLS heap use or real GPIO wiring on the board.

## Current communication and Wi-Fi sensing path

The camera and laptop communicate over the same home LAN. **MQTT over TLS**
carries events, telemetry and commands when MQTT firmware is active. **HTTP over
TCP/IP** carries video/snapshots and remains the legacy/fallback control path.

| Data | Connection | Format |
| --- | --- | --- |
| Live camera video | Laptop GET `http://ESP_IP:81/api/stream` | MJPEG: successive JPEG images in an HTTP multipart response |
| Status and sensor events | MQTT TLS port 8883; HTTP `/api/status`, `/api/events` fallback | JSON |
| Snapshot | Laptop GET port 80 `/api/snapshot` | JPEG |
| CSI pilot | ESP32 → laptop UDP 5005, when enabled | ADR-018 binary I/Q frames |
| Live controls | MQTT command + application ACK; HTTP port 80 fallback | JSON |
| Optional sensor callback | ESP32 POST to the configured laptop event URL | JSON over HTTP |
| Phone dashboard | Browser connects to laptop port 8000 | HTTP, SSE updates, relayed MJPEG |

There is no WebRTC or RTSP transport. CSI is available only after uploading
the pilot firmware and explicitly starting a session.
USB serial is used for uploading/debugging, not normal power-bank operation.

### CSI pilot with the existing ESP32-CAM

The **Wi-Fi sensing** panel is an experimental single-link pilot using the
current ESP32-CAM, its 2.4 GHz router connection and the laptop. The laptop may
stay on the router's 5 GHz band if both bands share the same LAN. No additional
CSI hardware is required to attempt capture. Human detection accuracy and useful
coverage are not established by the software tests.

Upload the complete **`reliable-1`** sketch. CSI is **off at every boot** and
configuration is runtime-only. No new firmware upload is needed to start/stop
subsequent sessions or change the laptop destination. The dashboard derives its
reachable LAN address when **Start pilot** is selected. A boot, Wi-Fi disconnect,
internal heap pressure or ten-minute lease ends capture; restart and recalibrate
afterward.

The firmware requests LLTF CSI from the associated AP only, stimulates responses
with bounded ICMP echo requests, and caps accepted frames at **10 Hz**. It does
not enable promiscuous capture, change channels or allocate an additional RTOS
worker stack. A four-slot **1,112-byte PSRAM queue** separates the Wi-Fi callback
from socket sending. The callback performs bounded framing/copying only.
Insufficient PSRAM or internal heap returns a start error. Capture requires at
least 8,192 free internal bytes and a 2,048-byte largest block at start; it stops
if sampled free space falls below 4,096 or the largest block below 1,536 bytes.
These guards reduce pressure. Older `csi-pilot-1` video/CSI coexistence testing
triggered a heap stop. Current `reliable-1` excludes simultaneous acquisition;
measure video and CSI in separate sessions.

Frames use RuView's **ADR-018** 20-byte little-endian header (`0xC5110001`) plus
32–256 bytes of ESP-IDF imaginary/real pairs, over **UDP 5005** by default. The
invalid four-byte prefix is zeroed and flagged. The laptop accepts only the
configured camera IP, node 1, current session sequence range and bounded frame
geometry; UDP is not cryptographically authenticated. This is a small receiver
and signal-analysis implementation, not a running RuView pose/identity model.
No simulated-data fallback exists in the dashboard.

The analysis normalizes per-frame subcarrier magnitudes by RMS amplitude and
aggregates the median change between adjacent frames into one-second windows.
A 60-second empty-room calibration requires at least 40 valid windows with at
least 5 samples per window. Its heuristic threshold uses the baseline median and
MAD, with a minimum 0.015. Two elevated windows assert **Elevated signal**; three below the baseline plus
40% of its gap to the threshold restore **Quiet signal**. Three seconds without data
means **No data**, never an empty-room claim. A channel/geometry change invalidates
the baseline. These are project heuristics, not a validated human classifier.

1. Keep the camera and router fixed. Upload `firmware/Doorbell/Doorbell.ino` with
   all sketch tabs, remove the IO0–GND jumper with power off, then reboot normally.
2. Refresh the dashboard and select **Wi-Fi → Start pilot**. Check actual packet
   rate; 10 Hz is a capture limit/target, not a guaranteed delivered rate.
3. If no packets arrive, check the displayed stop reason, the laptop destination,
   UDP port 5005/firewall and router ICMP responses. Do not substitute simulated
   results. `DOORBELL_CSI_PORT` changes the laptop's UDP listen port if necessary.
4. Leave the room and select **Calibrate empty room**. Keep the radio/video mode
   consistent during the 60-second baseline.
5. Expand **Record a pilot trial**. Record **empty room · 30s**, then a separate
   **walking · 30s** run with someone moving through the intended area. Export CSVs.
6. Keep video stopped throughout the CSI trial. Compare change scores, packet
   gaps, internal heap, MQTT availability and stream errors across repeated runs.
   Test non-human disturbances such as a fan before building any intrusion rule.
   Test video stability separately; this build excludes simultaneous CSI/video.

CSV and JSON summaries are stored locally under `.local-data/csi/`. Only aggregate
signal/health measurements are written, not raw I/Q frames or camera images.
A completed recording means its time elapsed; inspect valid-window counts to
judge data quality. The dashboard lists the latest six runs; up to 100 runs can be recorded before
export/removal is required. Interrupted runs are explicitly marked. Recordings
are manually labelled and do not establish ground-truth occupancy automatically.

Laptop endpoints: GET `/api/csi`, POST `/api/csi/start`, `/api/csi/stop`,
`/api/csi/calibrate`, `/api/csi/record-empty`, `/api/csi/record-walking`, and
GET `/api/csi/recordings/{id}`. The ESP32 accepts POST `/api/csi/config` with
`{"enabled":true,"host":"LAPTOP_LAN_IP","port":5005}` or `{"enabled":false}`.
Its HTTP/MQTT status contains `csi_pilot` counters, stop reason and session seed.

One radio link supplies anonymous signal-change evidence. A face sighting does
not by itself establish entry/exit, and CSI does not identify that person.
Named tracking is not implemented. The [experimental security watch](#experimental-security-watch)
uses sustained anonymous signal changes for local possible-intrusion incidents;
its human/intrusion accuracy remains unvalidated.

Physical pilot on November 12, 2026: the router and ESP32 were reported 1.5 m
apart in the same room. Real CSI arrived at approximately **14–16.4 Hz**, using
64 complex samples at 2,437 MHz, with zero receiver sequence gaps/rejections in
the observed intervals. Empty-room calibration completed. A sampled 60-second
interval with video receiving produced 1,653 additional stream frames, MQTT
online at every sample and no added stream errors.

On the older `csi-pilot-1` firmware, a later internal-heap guard stopped CSI while video was running. The camera
remained online. Boot low-water readings during the recorded trials fell to 1,472 bytes, so
long-running combined operation is not qualified. The guard was
kept; no successful continuous-operation claim is made.

After restarting CSI, **matched video-off** recordings each contained 30 valid
windows: mean normalized change **0.0219 empty** versus **0.1277 walking** (about
**5.8×**). These manually labelled, single-placement trials are evidence of a
signal difference, not detection accuracy, coverage radius, human identity or
proof of intrusion. The older video-on baseline is retained but not used for
that matched comparison. CSVs are downloadable under **Record a pilot trial**;
the dashboard shows a comparison only for completed runs with the same boot,
radio geometry and single video mode. Capture was stopped after the trials.

Implementation references: [Espressif router CSI example](https://github.com/espressif/esp-csi/tree/master/examples/get-started/csi_recv_router),
[ESP-IDF CSI callback guidance](https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32/api-guides/wifi.html#wi-fi-channel-state-information),
[RuView framing](https://github.com/ruvnet/RuView/blob/main/firmware/esp32-csi-node/main/csi_collector.c).

Developer verification uses `pytest` plus `scripts/check-csi.py`. The latter uses
**synthetic loopback UDP and an accelerated clock**, clearly separated from real
Wi-Fi measurements. It checks framing, calibration states, CSVs, stale-data
handling, controls and mobile layout; it does not validate physical sensing.

## Firmware upload

Open `firmware/Doorbell/Doorbell.ino` in Arduino IDE. Keep every file in that
sketch directory together. Select **AI Thinker ESP32-CAM**, choose the actual
USB serial port, and upload using the working USB-to-TTL wiring and IO0-to-GND
upload jumper. Remove that jumper with power disconnected after uploading.
Serial Monitor uses **115200 baud**. Wi-Fi settings live in the Git-excluded
`wifi_credentials.h`; use the example header when setting up a new checkout.
Do not paste passwords into logs or commit that local header.

## Sensor wiring

Disconnect power before wiring. Use a shared ground, preferably the power GND
beside 5V for the USB supply. Do not use the ambiguous corner GND/R pin.
Leave the microSD slot empty and do not initialize SD storage: GPIO13/14 are
shared with the SD interface.

| Sensor | Sensor pin | ESP32-CAM / supply |
| --- | --- | --- |
| HC-SR501 PIR | VCC | regulated 5V |
| HC-SR501 PIR | GND | common GND |
| HC-SR501 PIR | OUT | IO13 / GPIO13 |
| Four-pin sound detector | + | 3V3, provided the module supports 3.3V operation |
| Four-pin sound detector | G | common GND |
| Four-pin sound detector | D0 | IO14 / GPIO14 |
| Four-pin sound detector | A0 | leave disconnected |

ESP32 signal pins are 3.3V logic. Do not feed a 5V sound output into IO14.
The code reads a digital noise threshold, **not microphone audio**. Verify the
sound module's voltage specification before connecting it. For the usual LM393
module, adjust the potentiometer until its threshold LED reacts to a clap.
`doorbell_config.h` contains sensor enable switches, pins, active polarity and
timing. The default D0 trigger is LOW; change `DOORBELL_SOUND_ACTIVE_LOW` to
`false` if the module asserts HIGH. Unconnected sensor pins have pull resistors;
the firmware can be uploaded before the sensors are attached.

PIR motion is ignored for the first **60 seconds** after sensor initialization;
sound input settles for 2 seconds. Motion HIGH must be stable for 100ms.
The direct IRAM sound handler latches an edge and masks its interrupt for at
least 200 ms; the sensor task processes it and samples the pin every 20 ms.
Sound incidents require two seconds of quiet to rearm and at least three
seconds between accepted starts. Sensors continue running while Wi-Fi
reconnects or the laptop is unavailable.

## API v1

Replace `ESP_IP` with the address printed in Serial Monitor after each network
change. Control and JSON endpoints use port **80**. MJPEG uses port **81**.
This is a local-network prototype; these endpoints have no authentication.

| Method | URL | Result |
| --- | --- | --- |
| GET | `http://ESP_IP/api/status` | Wi-Fi, sensor readiness, activity window, stream clients, event/callback counters |
| GET | `http://ESP_IP/api/snapshot` | One JPEG image on demand |
| POST | `http://ESP_IP/api/live/start` | Set manual live request; return status |
| POST | `http://ESP_IP/api/live/stop` | Clear manual request; return status |
| GET | `http://ESP_IP:81/api/stream` | MJPEG while manual viewing or sensor activity is active; 409 if idle |
| GET | `http://ESP_IP/api/events?after=0` | Retained sensor events and next cursor |
| GET | `http://ESP_IP/api/config` | Current optional event-delivery URL |
| POST | `http://ESP_IP/api/config` | Save `{"event_url":"http://LAPTOP_IP:8000/api/events"}` in NVS |

The control server is separate from the stream server, so stop/status calls
can be serviced while video is streaming. Use one upstream stream connection
from the laptop server and distribute its frames there. Do not depend
on the ESP32 serving multiple simultaneous stream viewers.

An accepted PIR or sound incident opens a fixed **30-second activity window**;
further pulses and a PIR staying HIGH do not extend that active window.
After expiry, a newly accepted incident can open another window. Sound must
rearm through quiet first. Manual viewing remains on until stopped.
Stopping manual viewing does not cancel an active motion/sound window. When
both are inactive, the API stream ends. The camera stays initialized and Wi-Fi
stays connected; stopping a stream is not deep sleep or physical camera power-off.

The original camera page at `/`, `/capture`, `/status`, `/control` and
`:81/stream` still work. The original `:81/stream` is a direct manual view until
the client disconnects and is not controlled by `/api/live/stop`. The laptop
integration should use the new `:81/api/stream` endpoint.

## Laptop integration after uploading

No laptop server is required for the camera's own page or direct event polling.
This section describes optional HTTP callbacks. They remain disabled until an
event URL is configured; provisioned MQTT delivery is a separate primary path.

For a manual test from a laptop on the same network:

```bash
curl -X POST http://ESP_IP/api/live/start
# Open http://ESP_IP:81/api/stream in a browser.
curl -X POST http://ESP_IP/api/live/stop
curl 'http://ESP_IP/api/events?after=0'
```

To enable HTTP callbacks to the existing laptop server without another firmware upload:

```bash
curl -X POST http://ESP_IP/api/config \
  -H 'Content-Type: application/json' \
  -d '{"event_url":"http://LAPTOP_IP:8000/api/events"}'
```

The callback URL survives power loss. Clear it with `{"event_url":""}`.
Use a trusted LAN HTTP endpoint; HTTPS and credential-bearing URLs are not
supported in this prototype. Configuration contains no Wi-Fi passwords.

Each callback POST is a JSON event, for example:

```json
{"device_id":"esp32cam-example","boot_id":"1234abcd","id":1,"type":"motion","uptime_ms":61000}
```

The laptop must return a **2xx** response to acknowledge an event. Failed
delivery is retried after 5 seconds with bounded HTTP timeouts. Delivery is
at least once: deduplicate by `(device_id, boot_id, id)`. The ring retains the
newest **24 events in RAM** for HTTP polling/callbacks. Separately, the current
MQTT firmware persists up to 24 unconfirmed records in its NVS outbox for
power-loss replay; neither store contains video. An extended outage can
overwrite old events; status reports lost callback
events and polling reports `lost_before_cursor`.

For polling, track `boot_id` and `next_after`; reset the cursor to zero when
the boot ID changes. Poll about once per second if using events to open video.
A cursor beyond the newest ID returns `reset_required: true` and retained events.
The same ring is used for push and polling; neither consumes the other view.
`uptime_ms` is time since ESP32 startup, not UTC. Check event age and current
`stream_allowed` before treating a delayed event as current activity.

The ESP32 makes video available but does not independently record or upload
video when a sensor fires. The laptop server receives MQTT events, falls back
to HTTP polling, relays MJPEG and runs face recognition. Video recording is
not implemented.

## Verification boundary

Host tests exercise activity timing, sensor warm-up, overlap with manual viewing,
sound cooldown, millisecond-clock rollover and event-ring retention/cursors:

```bash
g++ -std=c++17 -Wall -Wextra -Werror tests/doorbell_logic_test.cpp -o /tmp/doorbell-logic-test
/tmp/doorbell-logic-test
```

Compile the sketch with the installed ESP32 core:

```bash
arduino-cli compile \
  --fqbn esp32:esp32:esp32cam:PartitionScheme=huge_app --jobs 2 firmware/Doorbell
```

These host checks alone do not validate real GPIO polarity, wiring, Wi-Fi
failover, NVS, HTTP callbacks, stream cancellation or camera operation. Physical
measurements and remaining gaps are listed under
[Reliability update](#reliability-update-reliable-1); the demo video is a separate
workflow recording. After a new upload, confirm `/api/status`, wait for
`pir_ready`, trigger each sensor, inspect `/api/events`, then check manual
start/stop and activity-window expiry. Optional HTTP callbacks can remain unset
when using MQTT and HTTP polling fallback.
