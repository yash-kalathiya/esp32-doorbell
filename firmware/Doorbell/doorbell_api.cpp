#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <cJSON.h>
#include <esp_system.h>
#include <errno.h>
#include "doorbell_api.h"
#include "doorbell_config.h"
#include "doorbell_logic.h"
#include "doorbell_mqtt.h"
#include "doorbell_diagnostics.h"
#include "doorbell_memory.h"

namespace {
DoorbellLogic logic(DOORBELL_ACTIVITY_MS, DOORBELL_PIR_WARMUP_MS,
                    DOORBELL_SOUND_SETTLE_MS, DOORBELL_SOUND_COOLDOWN_MS);
portMUX_TYPE stateMux = portMUX_INITIALIZER_UNLOCKED;
volatile bool soundPulse = false;
bool sensorTaskReady = false;
int streamClients = 0;
char eventUrl[192] = {};
uint32_t callbackAfter = 0, callbackLost = 0;
int callbackLastStatus = 0;
uint32_t configGeneration = 0;
Preferences preferences;
bool preferencesReady = false;
char deviceId[24] = {}, bootId[12] = {};

const char *eventName(DoorbellLogic::EventType type) {
  return type == DoorbellLogic::EventType::Motion ? "motion" : "sound";
}

void ARDUINO_ISR_ATTR soundInterrupt() {
  // Latch pulses only. No queue, networking, allocation or logging in the ISR.
  portENTER_CRITICAL_ISR(&stateMux);
  soundPulse = true;
  portEXIT_CRITICAL_ISR(&stateMux);
}

void sensorTask(void *) {
  for (;;) {
    bool pir = DOORBELL_PIR_ENABLED && digitalRead(DOORBELL_PIR_PIN) == HIGH;
    uint32_t now = millis();
    portENTER_CRITICAL(&stateMux);
    bool pulse = soundPulse;
    soundPulse = false;
    logic.observe(now, pir, pulse);
    portEXIT_CRITICAL(&stateMux);
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

esp_err_t jsonResponse(httpd_req_t *req, cJSON *json) {
  if (!json) return httpd_resp_send_500(req);
  char *body = cJSON_PrintUnformatted(json);
  cJSON_Delete(json);
  if (!body) return httpd_resp_send_500(req);
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  esp_err_t result = httpd_resp_send(req, body, HTTPD_RESP_USE_STRLEN);
  cJSON_free(body);
  return result;
}

esp_err_t jsonError(httpd_req_t *req, const char *status, const char *message) {
  httpd_resp_set_status(req, status);
  cJSON *json = cJSON_CreateObject();
  if (json) cJSON_AddStringToObject(json, "error", message);
  return jsonResponse(req, json);
}

cJSON *eventJson(const DoorbellLogic::Event &event) {
  cJSON *json = cJSON_CreateObject();
  if (!json) return nullptr;
  cJSON_AddStringToObject(json, "device_id", deviceId);
  cJSON_AddStringToObject(json, "boot_id", bootId);
  cJSON_AddNumberToObject(json, "id", event.id);
  cJSON_AddStringToObject(json, "type", eventName(event.type));
  cJSON_AddNumberToObject(json, "uptime_ms", event.uptimeMs);
  return json;
}

cJSON *statusJson() {
  bool manual, active, motion, ready;
  uint32_t remaining, newest, oldest, overwritten, delivered, lost;
  int clients, deliveryStatus;
  bool pushEnabled;
  uint32_t now = millis();
  portENTER_CRITICAL(&stateMux);
  manual = logic.manualRequested();
  active = logic.liveActive(now);
  motion = logic.stableMotion();
  ready = logic.motionReady();
  remaining = logic.activityRemaining(now);
  newest = logic.newestId();
  oldest = logic.oldestId();
  overwritten = logic.overwritten();
  delivered = callbackAfter;
  lost = callbackLost;
  deliveryStatus = callbackLastStatus;
  pushEnabled = eventUrl[0] != '\0';
  clients = streamClients;
  portEXIT_CRITICAL(&stateMux);
  cJSON *json = cJSON_CreateObject();
  if (!json) return nullptr;
  cJSON_AddNumberToObject(json, "api_version", 1);
  cJSON_AddStringToObject(json, "device_id", deviceId);
  cJSON_AddStringToObject(json, "boot_id", bootId);
  cJSON_AddNumberToObject(json, "uptime_ms", now);
  cJSON_AddBoolToObject(json, "wifi_connected", WiFi.status() == WL_CONNECTED);
  cJSON_AddStringToObject(json, "ip", WiFi.localIP().toString().c_str());
  cJSON_AddNumberToObject(json, "rssi", WiFi.RSSI());
  cJSON_AddBoolToObject(json, "stream_allowed", active);
  cJSON_AddBoolToObject(json, "manual_live", manual);
  cJSON_AddNumberToObject(json, "activity_remaining_ms", remaining);
  cJSON_AddNumberToObject(json, "stream_clients", clients);
  cJSON_AddBoolToObject(json, "sensor_task_ready", sensorTaskReady);
  cJSON_AddBoolToObject(json, "pir_enabled", DOORBELL_PIR_ENABLED);
  cJSON_AddNumberToObject(json, "pir_pin", DOORBELL_PIR_PIN);
  cJSON_AddBoolToObject(json, "pir_ready", DOORBELL_PIR_ENABLED && ready);
  cJSON_AddBoolToObject(json, "motion_active", motion);
  cJSON_AddBoolToObject(json, "sound_enabled", DOORBELL_SOUND_ENABLED);
  cJSON_AddNumberToObject(json, "sound_pin", DOORBELL_SOUND_PIN);
  cJSON_AddNumberToObject(json, "sound_input_level", DOORBELL_SOUND_ENABLED ? digitalRead(DOORBELL_SOUND_PIN) : -1);
  cJSON_AddBoolToObject(json, "sound_active_low", DOORBELL_SOUND_ACTIVE_LOW);
  cJSON_AddNumberToObject(json, "newest_event_id", newest);
  cJSON_AddNumberToObject(json, "oldest_event_id", oldest);
  cJSON_AddNumberToObject(json, "events_overwritten", overwritten);
  cJSON_AddBoolToObject(json, "event_push_enabled", pushEnabled);
  cJSON_AddNumberToObject(json, "callback_delivered_through", delivered);
  cJSON_AddNumberToObject(json, "callback_events_lost", lost);
  cJSON_AddNumberToObject(json, "callback_last_status", deliveryStatus);
  cJSON_AddStringToObject(json, "stream_path", ":81/api/stream");
  doorbellMqttStatus(json);
  doorbellDiagnosticsStatus(json);
  cJSON *diagnostics = cJSON_GetObjectItemCaseSensitive(json, "diagnostics");
  doorbellMemoryStatus(cJSON_GetObjectItemCaseSensitive(diagnostics, "memory"));
  return json;
}

esp_err_t apiStatus(httpd_req_t *req) { return jsonResponse(req, statusJson()); }

esp_err_t apiLiveStart(httpd_req_t *req) {
  portENTER_CRITICAL(&stateMux);
  logic.setManual(true);
  portEXIT_CRITICAL(&stateMux);
  return apiStatus(req);
}

esp_err_t apiLiveStop(httpd_req_t *req) {
  portENTER_CRITICAL(&stateMux);
  logic.setManual(false);
  portEXIT_CRITICAL(&stateMux);
  return apiStatus(req);
}

esp_err_t apiEvents(httpd_req_t *req) {
  uint32_t after = 0;
  size_t queryLen = httpd_req_get_url_query_len(req);
  if (queryLen) {
    char query[96], value[16];
    if (queryLen >= sizeof(query) || httpd_req_get_url_query_str(req, query, sizeof(query)) != ESP_OK)
      return jsonError(req, "400 Bad Request", "Invalid query");
    esp_err_t queryResult = httpd_query_key_value(query, "after", value, sizeof(value));
    if (queryResult == ESP_ERR_HTTPD_RESULT_TRUNC)
      return jsonError(req, "400 Bad Request", "after is too long");
    if (queryResult == ESP_OK) {
      char *end = nullptr;
      errno = 0;
      unsigned long parsed = strtoul(value, &end, 10);
      if (!value[0] || value[0] == '-' || *end || errno == ERANGE)
        return jsonError(req, "400 Bad Request", "after must be an unsigned event ID");
      after = parsed;
    }
  }
  DoorbellLogic::Event events[DoorbellLogic::capacity];
  uint32_t newest, oldest;
  size_t count;
  bool reset;
  portENTER_CRITICAL(&stateMux);
  newest = logic.newestId();
  oldest = logic.oldestId();
  reset = after > newest;
  if (reset) after = 0;
  count = logic.copyAfter(after, events, DoorbellLogic::capacity);
  portEXIT_CRITICAL(&stateMux);
  cJSON *json = cJSON_CreateObject();
  if (!json) return httpd_resp_send_500(req);
  cJSON_AddStringToObject(json, "device_id", deviceId);
  cJSON_AddStringToObject(json, "boot_id", bootId);
  cJSON_AddBoolToObject(json, "reset_required", reset);
  cJSON_AddNumberToObject(json, "lost_before_cursor", oldest > after + 1 ? oldest - after - 1 : 0);
  cJSON_AddNumberToObject(json, "next_after", count ? events[count - 1].id : newest);
  cJSON *array = cJSON_AddArrayToObject(json, "events");
  if (!array) { cJSON_Delete(json); return httpd_resp_send_500(req); }
  for (size_t i = 0; i < count; ++i) cJSON_AddItemToArray(array, eventJson(events[i]));
  return jsonResponse(req, json);
}

esp_err_t apiConfigGet(httpd_req_t *req) {
  char url[sizeof(eventUrl)];
  portENTER_CRITICAL(&stateMux);
  memcpy(url, eventUrl, sizeof(url));
  portEXIT_CRITICAL(&stateMux);
  cJSON *json = cJSON_CreateObject();
  if (json) {
    cJSON_AddStringToObject(json, "event_url", url);
    cJSON_AddBoolToObject(json, "persistent_storage_ready", preferencesReady);
  }
  return jsonResponse(req, json);
}

esp_err_t apiConfigPost(httpd_req_t *req) {
  char body[256];
  if (!req->content_len || req->content_len >= sizeof(body))
    return jsonError(req, "400 Bad Request", "Expected a small JSON object with event_url");
  size_t received = 0;
  while (received < req->content_len) {
    int n = httpd_req_recv(req, body + received, req->content_len - received);
    if (n <= 0) return jsonError(req, "408 Request Timeout", "Incomplete JSON body");
    received += n;
  }
  body[received] = '\0';
  cJSON *json = cJSON_ParseWithLengthOpts(body, received + 1, nullptr, true);
  cJSON *url = json ? cJSON_GetObjectItemCaseSensitive(json, "event_url") : nullptr;
  if (!cJSON_IsString(url)) {
    cJSON_Delete(json);
    return jsonError(req, "400 Bad Request", "event_url must be a string");
  }
  const char *value = url->valuestring;
  size_t len = strlen(value);
  if (len >= sizeof(eventUrl) || (len && (strncmp(value, "http://", 7) || len <= 7)) ||
      strpbrk(value, "\r\n\t @")) {
    cJSON_Delete(json);
    return jsonError(req, "400 Bad Request", "Use an http:// URL without credentials, or an empty string");
  }
  if (!preferencesReady || preferences.putString("event_url", value) != len ||
      preferences.getString("event_url", "<unset>") != value) {
    cJSON_Delete(json);
    return jsonError(req, "500 Internal Server Error", "Could not save configuration");
  }
  portENTER_CRITICAL(&stateMux);
  strlcpy(eventUrl, value, sizeof(eventUrl));
  callbackAfter = 0;
  callbackLost = 0;
  callbackLastStatus = 0;
  ++configGeneration;
  portEXIT_CRITICAL(&stateMux);
  cJSON_Delete(json);
  return apiConfigGet(req);
}

void registerRoute(httpd_handle_t server, const char *path, httpd_method_t method,
                   esp_err_t (*handler)(httpd_req_t *)) {
  httpd_uri_t uri = {};
  uri.uri = path;
  uri.method = method;
  uri.handler = handler;
  if (httpd_register_uri_handler(server, &uri) != ESP_OK)
    Serial.printf("Could not register doorbell route: %s\n", path);
}
}  // namespace

bool doorbellBegin() {
  if (sensorTaskReady) return true;
  snprintf(deviceId, sizeof(deviceId), "esp32cam-%08lx", (unsigned long)ESP.getEfuseMac());
  snprintf(bootId, sizeof(bootId), "%08lx", (unsigned long)esp_random());
  preferencesReady = preferences.begin("doorbell", false);
  String saved = preferencesReady ? preferences.getString("event_url", DOORBELL_DEFAULT_EVENT_URL)
                                  : String(DOORBELL_DEFAULT_EVENT_URL);
  strlcpy(eventUrl, saved.c_str(), sizeof(eventUrl));
  if (DOORBELL_PIR_ENABLED) pinMode(DOORBELL_PIR_PIN, INPUT_PULLDOWN);
  if (DOORBELL_SOUND_ENABLED) {
    pinMode(DOORBELL_SOUND_PIN, DOORBELL_SOUND_ACTIVE_LOW ? INPUT_PULLUP : INPUT_PULLDOWN);
    attachInterrupt(digitalPinToInterrupt(DOORBELL_SOUND_PIN), soundInterrupt,
                    DOORBELL_SOUND_ACTIVE_LOW ? FALLING : RISING);
  }
  logic.begin(millis());
  TaskHandle_t task = nullptr;
  sensorTaskReady = xTaskCreate(sensorTask, "doorbell-sensors", 3072, nullptr, 1, &task) == pdPASS;
  if (!sensorTaskReady) {
    if (DOORBELL_SOUND_ENABLED) detachInterrupt(digitalPinToInterrupt(DOORBELL_SOUND_PIN));
    Serial.println("Sensor task could not start; sensor events are unavailable.");
  }
  return sensorTaskReady;
}

const char *doorbellDeviceId() { return deviceId; }
const char *doorbellBootId() { return bootId; }
cJSON *doorbellStatusJson() { return statusJson(); }
void doorbellSetManual(bool on) {
  portENTER_CRITICAL(&stateMux);
  logic.setManual(on);
  portEXIT_CRITICAL(&stateMux);
}
bool doorbellNextEvent(uint32_t after, DoorbellLogic::Event *out) {
  portENTER_CRITICAL(&stateMux);
  bool found = logic.copyAfter(after, out, 1) != 0;
  portEXIT_CRITICAL(&stateMux);
  return found;
}

bool doorbellLiveActive() {
  uint32_t now = millis();
  portENTER_CRITICAL(&stateMux);
  bool active = logic.liveActive(now);
  portEXIT_CRITICAL(&stateMux);
  return active;
}

void doorbellStreamClient(bool opened) {
  portENTER_CRITICAL(&stateMux);
  if (opened) ++streamClients;
  else if (streamClients > 0) --streamClients;
  portEXIT_CRITICAL(&stateMux);
}

void doorbellRegisterApi(httpd_handle_t server) {
  registerRoute(server, "/api/status", HTTP_GET, apiStatus);
  registerRoute(server, "/api/events", HTTP_GET, apiEvents);
  registerRoute(server, "/api/live/start", HTTP_POST, apiLiveStart);
  registerRoute(server, "/api/live/stop", HTTP_POST, apiLiveStop);
  registerRoute(server, "/api/config", HTTP_GET, apiConfigGet);
  registerRoute(server, "/api/config", HTTP_POST, apiConfigPost);
}

void doorbellPoll() {
  static bool waiting = false;
  static uint32_t lastAttempt = 0;
  if (WiFi.status() != WL_CONNECTED || !sensorTaskReady) return;
  uint32_t now = millis();
  if (waiting && uint32_t(now - lastAttempt) < DOORBELL_CALLBACK_RETRY_MS) return;
  char url[sizeof(eventUrl)];
  DoorbellLogic::Event event;
  size_t found = 0;
  uint32_t generation;
  portENTER_CRITICAL(&stateMux);
  memcpy(url, eventUrl, sizeof(url));
  generation = configGeneration;
  if (url[0]) {
    uint32_t oldest = logic.oldestId();
    if (oldest > callbackAfter + 1) {
      callbackLost += oldest - callbackAfter - 1;
      callbackAfter = oldest - 1;
    }
    found = logic.copyAfter(callbackAfter, &event, 1);
  }
  portEXIT_CRITICAL(&stateMux);
  if (!url[0] || !found) return;
  cJSON *json = eventJson(event);
  if (!json) return;
  char *body = cJSON_PrintUnformatted(json);
  cJSON_Delete(json);
  if (!body) return;
  WiFiClient connection;
  HTTPClient http;
  http.setConnectTimeout(1000);
  http.setTimeout(1000);
  int status = -1;
  if (http.begin(connection, url)) {
    http.addHeader("Content-Type", "application/json");
    status = http.POST((uint8_t *)body, strlen(body));
    http.end();
  }
  cJSON_free(body);
  bool success = status >= 200 && status < 300;
  portENTER_CRITICAL(&stateMux);
  if (generation == configGeneration) {
    callbackLastStatus = status;
    if (success) callbackAfter = event.id;
  }
  portEXIT_CRITICAL(&stateMux);
  waiting = !success;
  lastAttempt = millis();
}
