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
#include "doorbell_csi.h"
#include "doorbell_levels.h"
#include <hal/gpio_ll.h>
#include <soc/gpio_struct.h>
#include <driver/gpio.h>
#include <esp_timer.h>
#include <esp_intr_alloc.h>

namespace {
DoorbellLogic logic(DOORBELL_ACTIVITY_MS, DOORBELL_PIR_WARMUP_MS,
                    DOORBELL_SOUND_SETTLE_MS, DOORBELL_SOUND_COOLDOWN_MS,
                    DOORBELL_SOUND_QUIET_MS, DOORBELL_SENSOR_FAULT_MS);
portMUX_TYPE stateMux = portMUX_INITIALIZER_UNLOCKED;
DoorbellLevel pirLevel, soundLevel;
struct SoundLatch { bool pending = false, seen = false, ended = false; uint32_t startMs = 0, endMs = 0; } soundLatch;
bool soundIrqMasked = false;
uint32_t soundIrqAt = 0, soundIrqCount = 0;
bool sensorTaskReady = false;
bool nightMode = false;
int streamClients = 0;
char eventUrl[192] = {};
uint32_t callbackAfter = 0, callbackLost = 0;
int callbackLastStatus = 0;
uint32_t configGeneration = 0;
Preferences preferences;
bool preferencesReady = false;
char deviceId[24] = {}, bootId[12] = {};

const char *eventName(DoorbellLogic::EventType type) {
  return type == DoorbellLogic::EventType::Motion || type == DoorbellLogic::EventType::MotionCleared ? "motion" : "sound";
}

inline __attribute__((always_inline)) void observeSound(bool high, uint32_t now) {
  bool active = high != DOORBELL_SOUND_ACTIVE_LOW;
  soundLevel.observe(high, now);
  if (active && !soundLatch.pending) {
    soundLatch.pending = true; soundLatch.seen = false; soundLatch.ended = false; soundLatch.startMs = now;
  } else if (!active && soundLatch.pending && !soundLatch.ended) {
    soundLatch.ended = true; soundLatch.endMs = now;
  }
}

void IRAM_ATTR soundInterrupt(void *) {
  // Mask this pin immediately: a comparator oscillation must not monopolize
  // the CPU. This inline register operation is safe with the flash cache off.
  gpio_ll_intr_disable(&GPIO, DOORBELL_SOUND_PIN);
  uint32_t now = static_cast<uint32_t>(esp_timer_get_time() / 1000);
  portENTER_CRITICAL_ISR(&stateMux);
  soundIrqMasked = true; soundIrqAt = now; ++soundIrqCount;
  observeSound(gpio_ll_get_level(&GPIO, DOORBELL_SOUND_PIN) != 0, now);
  portEXIT_CRITICAL_ISR(&stateMux);
}

void sensorTask(void *) {
  for (;;) {
    bool pir = DOORBELL_PIR_ENABLED && digitalRead(DOORBELL_PIR_PIN) == HIGH;
    uint32_t now = millis();
    portENTER_CRITICAL(&stateMux);
    pirLevel.observe(pir, now);
    bool soundHigh = !DOORBELL_SOUND_ENABLED || digitalRead(DOORBELL_SOUND_PIN) == HIGH;
    bool soundActive = DOORBELL_SOUND_ENABLED && (soundHigh != DOORBELL_SOUND_ACTIVE_LOW);
    if (DOORBELL_SOUND_ENABLED) observeSound(soundHigh, now);
    DoorbellLogic::SoundPulse pulse;
    pulse.sampled = true; pulse.active = soundActive;
    if (soundLatch.pending) {
      pulse.started = !soundLatch.seen; pulse.startedAt = soundLatch.startMs;
      pulse.ended = soundLatch.ended; pulse.endedAt = soundLatch.endMs;
      soundLatch.seen = true;
      if (soundLatch.ended) soundLatch.pending = false;
    }
    logic.observe(now, pir, pulse);
    bool rearm = soundIrqMasked && uint32_t(now-soundIrqAt) >= DOORBELL_SOUND_IRQ_HOLDOFF_MS;
    if (rearm) soundIrqMasked = false;
    portEXIT_CRITICAL(&stateMux);
    if (rearm) gpio_intr_enable(static_cast<gpio_num_t>(DOORBELL_SOUND_PIN));
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
  cJSON_AddStringToObject(json, "phase", static_cast<uint8_t>(event.type) >= 2 ? "cleared" : "triggered");
  cJSON_AddNumberToObject(json, "uptime_ms", event.uptimeMs);
  return json;
}

cJSON *statusJson() {
  bool manual, active, motion, ready, night;
  bool pirFault, soundFault, autoArmed;
  uint32_t suppressed, irqs;
  uint32_t remaining, newest, oldest, overwritten, delivered, lost;
  int clients, deliveryStatus;
  bool pushEnabled;
  DoorbellLevel::Snapshot pirTiming, soundTiming;
  uint32_t motionEvent, soundEvent, motionTriggers, soundTriggers;
  uint32_t now = millis();
  portENTER_CRITICAL(&stateMux);
  pirFault = logic.pirStuck(); soundFault = logic.soundNoisy(); autoArmed = logic.autoArmed();
  suppressed = logic.suppressedSoundCount(); irqs = soundIrqCount;
  now = millis();
  night = nightMode;
  pirTiming = pirLevel.snapshot(); soundTiming = soundLevel.snapshot();
  motionEvent = logic.motionActiveEvent(); soundEvent = logic.soundActiveEvent();
  motionTriggers = logic.motionTriggerCount(); soundTriggers = logic.soundTriggerCount();
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
  cJSON *policy = cJSON_AddObjectToObject(json, "sensor_policy");
  if (policy) {
    cJSON_AddNumberToObject(policy, "version", 1);
    cJSON_AddNumberToObject(policy, "auto_limit_ms", DOORBELL_ACTIVITY_MS);
    cJSON_AddNumberToObject(policy, "sound_quiet_ms", DOORBELL_SOUND_QUIET_MS);
    cJSON_AddBoolToObject(policy, "auto_armed", autoArmed);
    cJSON_AddBoolToObject(policy, "pir_stuck_high", pirFault);
    cJSON_AddBoolToObject(policy, "sound_noisy", soundFault);
    cJSON_AddNumberToObject(policy, "sound_suppressed", suppressed);
    cJSON_AddNumberToObject(policy, "sound_interrupts", irqs);
    cJSON_AddNumberToObject(policy, "sound_irq_holdoff_ms", DOORBELL_SOUND_IRQ_HOLDOFF_MS);
  }
  cJSON_AddNumberToObject(json, "stream_clients", clients);
  cJSON_AddBoolToObject(json, "night_mode", night);
  cJSON_AddNumberToObject(json, "camera_light_percent", doorbellCameraLightPercent());
  cJSON_AddNumberToObject(json, "sensor_timing_version", 1);
  cJSON *levels = cJSON_AddObjectToObject(json, "sensor_levels");
  if (levels) {
    cJSON_AddItemToObject(levels, "motion", doorbellLevelJson(pirTiming, now, true, motionEvent, motionTriggers));
    cJSON_AddItemToObject(levels, "sound", doorbellLevelJson(soundTiming, now, !DOORBELL_SOUND_ACTIVE_LOW, soundEvent, soundTriggers));
  }
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
  doorbellCsiStatus(json);
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

esp_err_t setNightMode(httpd_req_t *req, bool enabled) {
  bool before;
  portENTER_CRITICAL(&stateMux); before = nightMode; portEXIT_CRITICAL(&stateMux);
  if (doorbellCameraNightMode(enabled) != ESP_OK) {
    doorbellCameraNightMode(before);
    return jsonError(req, "500 Internal Server Error", "Camera night settings could not be applied");
  }
  if (enabled != before && (!preferencesReady || preferences.putBool("night_mode", enabled) != sizeof(bool)
      || preferences.getBool("night_mode", !enabled) != enabled)) {
    doorbellCameraNightMode(before);
    return jsonError(req, "500 Internal Server Error", "Camera night settings could not be saved");
  }
  portENTER_CRITICAL(&stateMux); nightMode = enabled; portEXIT_CRITICAL(&stateMux);
  return apiStatus(req);
}
esp_err_t apiLightStart(httpd_req_t *req) { doorbellCameraLight(true); return apiStatus(req); }
esp_err_t apiLightStop(httpd_req_t *req) { doorbellCameraLight(false); return apiStatus(req); }
esp_err_t apiNightStart(httpd_req_t *req) { return setNightMode(req, true); }
esp_err_t apiNightStop(httpd_req_t *req) { return setNightMode(req, false); }

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

esp_err_t apiCsiConfig(httpd_req_t *req) {
  char body[160];
  if (req->content_len <= 0 || req->content_len >= sizeof(body))
    return jsonError(req, "400 Bad Request", "Expected enabled, host and port");
  size_t used = 0;
  while (used < req->content_len) {
    int n = httpd_req_recv(req, body+used, req->content_len-used);
    if (n <= 0) return jsonError(req, "408 Request Timeout", "Incomplete CSI configuration");
    used += n;
  }
  body[used] = 0;
  cJSON *json = cJSON_ParseWithLengthOpts(body, used+1, nullptr, true);
  cJSON *on = cJSON_GetObjectItemCaseSensitive(json, "enabled");
  cJSON *host = cJSON_GetObjectItemCaseSensitive(json, "host");
  cJSON *port = cJSON_GetObjectItemCaseSensitive(json, "port");
  bool start = cJSON_IsTrue(on);
  if (!cJSON_IsBool(on) || (start && (!cJSON_IsString(host) || !cJSON_IsNumber(port) || port->valuedouble != port->valueint || port->valueint <= 0 || port->valueint > 65535))) {
    cJSON_Delete(json); return jsonError(req, "400 Bad Request", "Invalid CSI configuration");
  }
  portENTER_CRITICAL(&stateMux); bool streaming = streamClients > 0; portEXIT_CRITICAL(&stateMux);
  if (start && streaming) {
    cJSON_Delete(json); return jsonError(req, "409 Conflict", "Stop live video before starting CSI");
  }
  esp_err_t result = doorbellCsiConfigure(start, start ? host->valuestring : "", start ? port->valueint : 0);
  cJSON_Delete(json);
  if (result != ESP_OK) return jsonError(req, "503 Service Unavailable", "CSI could not start; check WiFi, PSRAM and destination");
  return apiStatus(req);
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
  nightMode = preferencesReady && preferences.getBool("night_mode", false);
  if (doorbellCameraNightMode(nightMode) != ESP_OK) Serial.println("Camera night settings could not be restored");
  String saved = preferencesReady ? preferences.getString("event_url", DOORBELL_DEFAULT_EVENT_URL)
                                  : String(DOORBELL_DEFAULT_EVENT_URL);
  strlcpy(eventUrl, saved.c_str(), sizeof(eventUrl));
  if (DOORBELL_PIR_ENABLED) pinMode(DOORBELL_PIR_PIN, INPUT_PULLDOWN);
  if (DOORBELL_SOUND_ENABLED) {
    pinMode(DOORBELL_SOUND_PIN, DOORBELL_SOUND_ACTIVE_LOW ? INPUT_PULLUP : INPUT_PULLDOWN);
  }
  uint32_t started = millis();
  pirLevel.observe(DOORBELL_PIR_ENABLED && digitalRead(DOORBELL_PIR_PIN) == HIGH, started);
  soundLevel.observe(!DOORBELL_SOUND_ENABLED || digitalRead(DOORBELL_SOUND_PIN) == HIGH, started);
  logic.begin(started);
  if (DOORBELL_SOUND_ENABLED) {
    // The camera shares this service with IRAM VSYNC handlers. Arduino's
    // attachInterrupt trampoline is flash-resident in the installed SDK.
    esp_err_t irq = gpio_install_isr_service(ESP_INTR_FLAG_IRAM);
    if (irq != ESP_OK && irq != ESP_ERR_INVALID_STATE) return false;
    if (gpio_set_intr_type(static_cast<gpio_num_t>(DOORBELL_SOUND_PIN), GPIO_INTR_ANYEDGE) != ESP_OK ||
        gpio_isr_handler_add(static_cast<gpio_num_t>(DOORBELL_SOUND_PIN), soundInterrupt, nullptr) != ESP_OK) return false;
  }
  TaskHandle_t task = nullptr;
  sensorTaskReady = xTaskCreate(sensorTask, "doorbell-sensors", 3072, nullptr, 1, &task) == pdPASS;
  if (!sensorTaskReady) {
    if (DOORBELL_SOUND_ENABLED) gpio_isr_handler_remove(static_cast<gpio_num_t>(DOORBELL_SOUND_PIN));
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
  registerRoute(server, "/api/light/start", HTTP_POST, apiLightStart);
  registerRoute(server, "/api/light/stop", HTTP_POST, apiLightStop);
  registerRoute(server, "/api/night/start", HTTP_POST, apiNightStart);
  registerRoute(server, "/api/night/stop", HTTP_POST, apiNightStop);
  registerRoute(server, "/api/csi/config", HTTP_POST, apiCsiConfig);
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
