#include <Arduino.h>
#include <WiFi.h>
#include <esp_heap_caps.h>
#include <esp_system.h>
#include <time.h>
#include "doorbell_diagnostics.h"

namespace {
constexpr uint32_t INTERNAL = MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT;
portMUX_TYPE diagnosticsMux = portMUX_INITIALIZER_UNLOCKED;
struct Diagnostics {
  uint32_t wifiConnections = 0, wifiDisconnects = 0, wifiDisconnectedAt = 0;
  int wifiReason = 0;
  uint32_t attempts = 0, connections = 0, disconnects = 0, errors = 0;
  uint32_t mqttEventAt = 0, errorAt = 0, errorFree = 0, errorLargest = 0;
  int mqttEvent = 0, errorType = 0, espError = 0, tlsError = 0;
  int verifyFlags = 0, socketError = 0, brokerCode = 0;
  uint32_t captures = 0, captureFailures = 0, capturesInFlight = 0;
  uint32_t captureStartedAt = 0, captureMs = 0, captureMaxMs = 0;
  uint32_t streamFrames = 0, streamErrors = 0, streamFrameAt = 0;
} diagnostics;

const char *resetName(esp_reset_reason_t reason) {
  switch (reason) {
    case ESP_RST_POWERON: return "power_on";
    case ESP_RST_EXT: return "external";
    case ESP_RST_SW: return "software";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: return "interrupt_watchdog";
    case ESP_RST_TASK_WDT: return "task_watchdog";
    case ESP_RST_WDT: return "watchdog";
    case ESP_RST_DEEPSLEEP: return "deep_sleep";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SDIO: return "sdio";
    default: return "unknown";
  }
}
}  // namespace

void doorbellDiagnosticsBegin() {
  WiFi.onEvent([](arduino_event_id_t event, arduino_event_info_t info) {
    uint32_t now = millis();
    if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
      portENTER_CRITICAL(&diagnosticsMux);
      ++diagnostics.wifiConnections;
      portEXIT_CRITICAL(&diagnosticsMux);
    } else if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
      int reason = info.wifi_sta_disconnected.reason;
      portENTER_CRITICAL(&diagnosticsMux);
      ++diagnostics.wifiDisconnects;
      diagnostics.wifiReason = reason;
      diagnostics.wifiDisconnectedAt = now;
      portEXIT_CRITICAL(&diagnosticsMux);
      Serial.printf("[diag] WiFi disconnected: reason=%d uptime=%lu\n", reason, (unsigned long)now);
    }
  });
  Serial.printf("[diag] firmware=mqtt-diag-1 reset=%s\n", resetName(esp_reset_reason()));
}

void doorbellDiagnosticsMqttAttempt() {
  portENTER_CRITICAL(&diagnosticsMux);
  ++diagnostics.attempts;
  portEXIT_CRITICAL(&diagnosticsMux);
}

void doorbellDiagnosticsMqttConnected() {
  uint32_t now = millis();
  portENTER_CRITICAL(&diagnosticsMux);
  ++diagnostics.connections;
  diagnostics.mqttEvent = 1;
  diagnostics.mqttEventAt = now;
  // Preserve the last error and its heap snapshot across successful reconnects.
  portEXIT_CRITICAL(&diagnosticsMux);
  Serial.printf("[diag] MQTT connected: uptime=%lu\n", (unsigned long)now);
}

void doorbellDiagnosticsMqttDisconnected() {
  uint32_t now = millis();
  portENTER_CRITICAL(&diagnosticsMux);
  ++diagnostics.disconnects;
  diagnostics.mqttEvent = 2;
  diagnostics.mqttEventAt = now;
  portEXIT_CRITICAL(&diagnosticsMux);
  Serial.printf("[diag] MQTT disconnected: uptime=%lu\n", (unsigned long)now);
}

void doorbellDiagnosticsMqttError(int type, int espError, int tlsError,
                                 int verifyFlags, int socketError, int brokerCode) {
  uint32_t now = millis();
  uint32_t free = heap_caps_get_free_size(INTERNAL);
  uint32_t largest = heap_caps_get_largest_free_block(INTERNAL);
  portENTER_CRITICAL(&diagnosticsMux);
  ++diagnostics.errors;
  diagnostics.mqttEvent = 3;
  diagnostics.mqttEventAt = diagnostics.errorAt = now;
  diagnostics.errorType = type;
  diagnostics.espError = espError;
  diagnostics.tlsError = tlsError;
  diagnostics.verifyFlags = verifyFlags;
  diagnostics.socketError = socketError;
  diagnostics.brokerCode = brokerCode;
  diagnostics.errorFree = free;
  diagnostics.errorLargest = largest;
  portEXIT_CRITICAL(&diagnosticsMux);
  Serial.printf("[diag] MQTT error: type=%d esp=0x%x tls=%d verify=0x%x socket=%d broker=%d free=%lu largest=%lu\n",
                type, (unsigned int)espError, tlsError, (unsigned int)verifyFlags, socketError, brokerCode,
                (unsigned long)free, (unsigned long)largest);
}

void doorbellDiagnosticsCaptureBegin() {
  uint32_t now = millis();
  portENTER_CRITICAL(&diagnosticsMux);
  ++diagnostics.capturesInFlight;
  diagnostics.captureStartedAt = now;
  portEXIT_CRITICAL(&diagnosticsMux);
}

void doorbellDiagnosticsCaptureEnd(bool success, uint32_t elapsedMs) {
  portENTER_CRITICAL(&diagnosticsMux);
  if (diagnostics.capturesInFlight) --diagnostics.capturesInFlight;
  ++diagnostics.captures;
  if (!success) ++diagnostics.captureFailures;
  diagnostics.captureMs = elapsedMs;
  if (elapsedMs > diagnostics.captureMaxMs) diagnostics.captureMaxMs = elapsedMs;
  portEXIT_CRITICAL(&diagnosticsMux);
}

void doorbellDiagnosticsStream(bool success) {
  uint32_t now = millis();
  portENTER_CRITICAL(&diagnosticsMux);
  if (success) { ++diagnostics.streamFrames; diagnostics.streamFrameAt = now; }
  else ++diagnostics.streamErrors;
  portEXIT_CRITICAL(&diagnosticsMux);
}

void doorbellDiagnosticsStatus(cJSON *json) {
  Diagnostics d;
  portENTER_CRITICAL(&diagnosticsMux);
  d = diagnostics;
  portEXIT_CRITICAL(&diagnosticsMux);
  cJSON_AddStringToObject(json, "firmware_version", "mqtt-diag-1");
  cJSON *root = cJSON_AddObjectToObject(json, "diagnostics");
  if (!root) return;
  cJSON_AddNumberToObject(root, "version", 1);
  cJSON_AddNumberToObject(root, "reset_reason", esp_reset_reason());
  cJSON_AddStringToObject(root, "reset_name", resetName(esp_reset_reason()));
  cJSON_AddNumberToObject(root, "unix_time", time(nullptr));
  cJSON *memory = cJSON_AddObjectToObject(root, "memory");
  if (memory) {
    cJSON_AddNumberToObject(memory, "internal_free_bytes", heap_caps_get_free_size(INTERNAL));
    cJSON_AddNumberToObject(memory, "internal_min_free_bytes", heap_caps_get_minimum_free_size(INTERNAL));
    cJSON_AddNumberToObject(memory, "internal_largest_block_bytes", heap_caps_get_largest_free_block(INTERNAL));
    cJSON_AddNumberToObject(memory, "psram_free_bytes", heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
  }
  cJSON *wifi = cJSON_AddObjectToObject(root, "wifi");
  if (wifi) {
    cJSON_AddNumberToObject(wifi, "ip_connections", d.wifiConnections);
    cJSON_AddNumberToObject(wifi, "disconnects", d.wifiDisconnects);
    cJSON_AddNumberToObject(wifi, "last_disconnect_reason", d.wifiReason);
    cJSON_AddNumberToObject(wifi, "last_disconnect_uptime_ms", d.wifiDisconnectedAt);
  }
  cJSON *mqtt = cJSON_AddObjectToObject(root, "mqtt");
  if (mqtt) {
    cJSON_AddNumberToObject(mqtt, "attempts", d.attempts);
    cJSON_AddNumberToObject(mqtt, "connections", d.connections);
    cJSON_AddNumberToObject(mqtt, "disconnects", d.disconnects);
    cJSON_AddNumberToObject(mqtt, "errors", d.errors);
    const char *events[] = {"none", "connected", "disconnected", "error"};
    cJSON_AddStringToObject(mqtt, "last_event", events[d.mqttEvent]);
    cJSON_AddNumberToObject(mqtt, "last_event_uptime_ms", d.mqttEventAt);
    cJSON *error = d.errors ? cJSON_AddObjectToObject(mqtt, "last_error") : nullptr;
    if (error) {
      cJSON_AddNumberToObject(error, "uptime_ms", d.errorAt);
      cJSON_AddNumberToObject(error, "type", d.errorType);
      cJSON_AddNumberToObject(error, "esp_error", d.espError);
      cJSON_AddNumberToObject(error, "tls_error", d.tlsError);
      cJSON_AddNumberToObject(error, "certificate_flags", d.verifyFlags);
      cJSON_AddNumberToObject(error, "socket_errno", d.socketError);
      cJSON_AddNumberToObject(error, "broker_code", d.brokerCode);
      cJSON_AddNumberToObject(error, "internal_free_bytes", d.errorFree);
      cJSON_AddNumberToObject(error, "internal_largest_block_bytes", d.errorLargest);
    } else if (!d.errors) cJSON_AddNullToObject(mqtt, "last_error");
  }
  cJSON *camera = cJSON_AddObjectToObject(root, "camera");
  if (camera) {
    cJSON_AddNumberToObject(camera, "captures", d.captures);
    cJSON_AddNumberToObject(camera, "capture_failures", d.captureFailures);
    cJSON_AddNumberToObject(camera, "captures_in_flight", d.capturesInFlight);
    cJSON_AddNumberToObject(camera, "last_capture_started_ms", d.captureStartedAt);
    cJSON_AddNumberToObject(camera, "last_capture_duration_ms", d.captureMs);
    cJSON_AddNumberToObject(camera, "max_capture_duration_ms", d.captureMaxMs);
    cJSON_AddNumberToObject(camera, "stream_frames", d.streamFrames);
    cJSON_AddNumberToObject(camera, "stream_errors", d.streamErrors);
    cJSON_AddNumberToObject(camera, "last_stream_frame_uptime_ms", d.streamFrameAt);
  }
}
