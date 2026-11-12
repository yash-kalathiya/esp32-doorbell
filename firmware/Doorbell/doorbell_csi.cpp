#include <Arduino.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_system.h>
#include <esp_heap_caps.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>
#include <lwip/sockets.h>
#include <lwip/inet_chksum.h>
#include <fcntl.h>
#include <unistd.h>
#include "doorbell_csi.h"
#include "doorbell_csi_wire.h"

namespace {
constexpr unsigned slots = 4, intervalMs = 100, leaseMs = 600000;
struct Frame { uint16_t length; uint8_t bytes[DoorbellCsiWire::headerBytes + DoorbellCsiWire::maxIqBytes]; };
static_assert(sizeof(Frame) == 278, "Bound CSI queue storage");
StaticQueue_t queueControl;
StaticSemaphore_t controlBuffer;
SemaphoreHandle_t control = nullptr;
QueueHandle_t queue = nullptr;
portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
bool enabled = false, mustStop = false, subscribed = false;
bool starting = false;
unsigned cameraUsers = 0;
const char *stopReason = "Disabled at boot";
uint32_t lastHealth = 0;
uint8_t apMac[6] = {};
uint32_t sequence = 0, seed = 0, captured = 0, dropped = 0, sent = 0, errors = 0;
uint32_t lastCapture = 0, started = 0, lastPing = 0;
int udpFd = -1, pingFd = -1;
sockaddr_in destination = {}, gateway = {};
char destinationHost[16] = {};
unsigned destinationPort = 0;

void receiveCsi(void *, wifi_csi_info_t *info) {
  if (!info || !info->buf) return;
  uint32_t now = millis(), next;
  portENTER_CRITICAL(&mux);
  if (!enabled || memcmp(info->mac, apMac, 6) || uint32_t(now-lastCapture) < intervalMs) {
    portEXIT_CRITICAL(&mux); return;
  }
  lastCapture = now; next = sequence++; ++captured;
  portEXIT_CRITICAL(&mux);
  Frame frame;
  frame.length = DoorbellCsiWire::encode(frame.bytes, info->buf, info->len,
    info->rx_ctrl.channel, next, info->rx_ctrl.rssi, info->rx_ctrl.noise_floor,
    info->first_word_invalid, info->rx_ctrl.cwb);
  // Wi-Fi callback: bounded copy only. No sockets, JSON, heap or logging.
  if (!frame.length || xQueueSend(queue, &frame, 0) != pdTRUE) {
    portENTER_CRITICAL(&mux); ++dropped; portEXIT_CRITICAL(&mux);
  }
}

void stop(const char *reason = "Stopped") {
  portENTER_CRITICAL(&mux); stopReason = reason; enabled = false; portEXIT_CRITICAL(&mux);
  esp_wifi_set_csi(false);
  if (udpFd >= 0) { close(udpFd); udpFd = -1; }
  if (pingFd >= 0) { close(pingFd); pingFd = -1; }
  if (queue) xQueueReset(queue);
}
}

static esp_err_t configureLocked(bool on, const char *host, unsigned port) {
  if (!on) { stop(); return ESP_OK; }
  in_addr address;
  if (!host || !port || port > 65535 || inet_aton(host, &address) == 0) return ESP_ERR_INVALID_ARG;
  uint32_t ip = ntohl(address.s_addr);
  bool local = (ip >> 24 == 10) || (ip >> 20 == 0xAC1) || (ip >> 16 == 0xC0A8);
  if (!local || (ip & 255) == 255) return ESP_ERR_INVALID_ARG;
  if (!subscribed) {
    WiFi.onEvent([](arduino_event_id_t event, arduino_event_info_t) {
      if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
        portENTER_CRITICAL(&mux); mustStop = true; portEXIT_CRITICAL(&mux);
      }
    });
    subscribed = true;
  }
  portENTER_CRITICAL(&mux); mustStop = false; portEXIT_CRITICAL(&mux);
  wifi_ap_record_t ap = {};
  if (WiFi.status() != WL_CONNECTED || esp_wifi_sta_get_ap_info(&ap) != ESP_OK) return ESP_ERR_INVALID_STATE;
  stop();
  if (heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT) < 8192 ||
      heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT) < 2048) return ESP_ERR_NO_MEM;
  if (!queue) {
    auto storage = static_cast<uint8_t *>(heap_caps_malloc(slots*sizeof(Frame), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!storage) return ESP_ERR_NO_MEM;
    queue = xQueueCreateStatic(slots, sizeof(Frame), storage, &queueControl);
    if (!queue) { heap_caps_free(storage); return ESP_ERR_NO_MEM; }
  }
  udpFd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
  pingFd = socket(AF_INET, SOCK_RAW, IPPROTO_ICMP);
  if (udpFd < 0 || pingFd < 0 || fcntl(udpFd, F_SETFL, O_NONBLOCK) < 0 || fcntl(pingFd, F_SETFL, O_NONBLOCK) < 0) {
    stop(); return ESP_FAIL;
  }
  destination = {}; destination.sin_family = AF_INET; destination.sin_port = htons(port); destination.sin_addr = address;
  gateway = {}; gateway.sin_family = AF_INET; gateway.sin_addr.s_addr = uint32_t(WiFi.gatewayIP());
  wifi_csi_config_t config = {};
  config.lltf_en = true; config.htltf_en = false; config.stbc_htltf2_en = false;
  config.ltf_merge_en = true; config.channel_filter_en = true;
  config.manu_scale = true; config.shift = 1;
  esp_err_t result = esp_wifi_set_csi_config(&config);
  if (result == ESP_OK) result = esp_wifi_set_csi_rx_cb(receiveCsi, nullptr);
  if (result == ESP_OK) result = esp_wifi_set_csi(true);
  if (result != ESP_OK) { stop(); return result; }
  portENTER_CRITICAL(&mux);
  memcpy(apMac, ap.bssid, sizeof(apMac)); seed = esp_random(); sequence = seed;
  captured = dropped = sent = errors = 0; started = millis(); lastCapture = started; lastPing = started;
  strlcpy(destinationHost, host, sizeof(destinationHost)); destinationPort = port;
  stopReason = "Capture active"; lastHealth = started; enabled = true;
  portEXIT_CRITICAL(&mux);
  return ESP_OK;
}

esp_err_t doorbellCsiConfigure(bool on, const char *host, unsigned port) {
  if (!control) control = xSemaphoreCreateMutexStatic(&controlBuffer);
  if (!control || xSemaphoreTake(control, pdMS_TO_TICKS(1000)) != pdTRUE) return ESP_ERR_TIMEOUT;
  portENTER_CRITICAL(&mux);
  bool busy = on && cameraUsers > 0;
  starting = on && !busy;
  portEXIT_CRITICAL(&mux);
  if (busy) { xSemaphoreGive(control); return ESP_ERR_INVALID_STATE; }
  esp_err_t result = configureLocked(on, host, port);
  portENTER_CRITICAL(&mux); starting = false; portEXIT_CRITICAL(&mux);
  xSemaphoreGive(control);
  return result;
}

void doorbellCsiPoll() {
  if (!control || xSemaphoreTake(control, 0) != pdTRUE) return;
  portENTER_CRITICAL(&mux); bool on = enabled, disconnected = mustStop; uint32_t began = started; portEXIT_CRITICAL(&mux);
  if (!on) { xSemaphoreGive(control); return; }
  if (disconnected || WiFi.status() != WL_CONNECTED || uint32_t(millis()-began) >= leaseMs) {
    stop(disconnected || WiFi.status() != WL_CONNECTED ? "WiFi disconnected" : "Ten-minute lease ended");
    xSemaphoreGive(control); return;
  }
  if (uint32_t(millis()-lastHealth) >= 1000) {
    lastHealth = millis();
    if (heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT) < 4096 ||
        heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT) < 1536) {
      stop("Internal heap pressure"); xSemaphoreGive(control); return;
    }
  }
  Frame frame;
  for (unsigned i=0; i<slots && xQueueReceive(queue, &frame, 0) == pdTRUE; ++i) {
    bool ok = sendto(udpFd, frame.bytes, frame.length, MSG_DONTWAIT,
                    reinterpret_cast<sockaddr *>(&destination), sizeof(destination)) == frame.length;
    portENTER_CRITICAL(&mux); if (ok) ++sent; else ++errors; portEXIT_CRITICAL(&mux);
  }
  if (uint32_t(millis()-lastPing) >= intervalMs) {
    lastPing = millis();
    // Raw ICMP avoids allocating another ping task/stack alongside TLS/video.
    uint8_t echo[8] = {8, 0, 0, 0, 0xC5, 0x11, uint8_t(lastPing >> 8), uint8_t(lastPing)};
    uint16_t checksum = inet_chksum(echo, sizeof(echo)); memcpy(echo+2, &checksum, 2);
    sendto(pingFd, echo, sizeof(echo), MSG_DONTWAIT, reinterpret_cast<sockaddr *>(&gateway), sizeof(gateway));
  }
  uint8_t discard[96];
  for (unsigned i=0; i<slots; ++i) if (recv(pingFd, discard, sizeof(discard), MSG_DONTWAIT) < 0) break;
  xSemaphoreGive(control);
}

bool doorbellCsiActive() {
  portENTER_CRITICAL(&mux); bool active = enabled; portEXIT_CRITICAL(&mux);
  return active;
}

bool doorbellCsiCameraBegin() {
  portENTER_CRITICAL(&mux);
  bool accepted = !enabled && !starting;
  if (accepted) ++cameraUsers;
  portEXIT_CRITICAL(&mux);
  return accepted;
}

void doorbellCsiCameraEnd() {
  portENTER_CRITICAL(&mux); if (cameraUsers) --cameraUsers; portEXIT_CRITICAL(&mux);
}

void doorbellCsiStatus(cJSON *root) {
  bool on; uint32_t initial, received, lost, delivered, failed, remaining;
  char host[16]; unsigned port; const char *reason;
  portENTER_CRITICAL(&mux);
  reason = stopReason; on = enabled; initial = seed; received = captured; lost = dropped; delivered = sent; failed = errors;
  uint32_t age = millis()-started;
  remaining = on && age < leaseMs ? leaseMs-age : 0;
  strlcpy(host, destinationHost, sizeof(host)); port = destinationPort;
  portEXIT_CRITICAL(&mux);
  cJSON *json = cJSON_AddObjectToObject(root, "csi_pilot");
  if (!json) return;
  cJSON_AddStringToObject(json, "reason", reason);
  cJSON_AddBoolToObject(json, "supported", true); cJSON_AddBoolToObject(json, "enabled", on);
  cJSON_AddBoolToObject(json, "exclusive_camera", true);
  cJSON_AddStringToObject(json, "host", host); cJSON_AddNumberToObject(json, "port", port);
  cJSON_AddNumberToObject(json, "sequence_start", initial); cJSON_AddNumberToObject(json, "target_hz", 1000/intervalMs);
  cJSON_AddNumberToObject(json, "captured", received); cJSON_AddNumberToObject(json, "queue_drops", lost);
  cJSON_AddNumberToObject(json, "sent", delivered); cJSON_AddNumberToObject(json, "send_errors", failed);
  cJSON_AddNumberToObject(json, "lease_remaining_ms", remaining);
}
