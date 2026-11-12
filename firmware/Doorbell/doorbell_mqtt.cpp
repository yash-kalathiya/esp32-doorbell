#include <Arduino.h>
#include <WiFi.h>
#include <Preferences.h>
#include <mqtt_client.h>
#include <sys/time.h>
#include <time.h>
#include <math.h>
#include <esp_heap_caps.h>
#include "doorbell_api.h"
#include "doorbell_mqtt.h"
#include "doorbell_delivery.h"
#include "doorbell_diagnostics.h"
#include "doorbell_memory.h"

#if __has_include("mqtt_credentials.h")
#include "mqtt_credentials.h"
#define DOORBELL_HAS_MQTT 1
#else
#define DOORBELL_HAS_MQTT 0
#endif

namespace {
volatile bool configured = false, connected = false, persistent = false;
volatile uint32_t pending = 0, lost = 0, rejected = 0;
volatile uint32_t publishFailures = 0;
bool inboxPsram = false;
#if DOORBELL_HAS_MQTT
esp_mqtt_client_handle_t client = nullptr;
QueueHandle_t inbox = nullptr;
StaticQueue_t inboxControl;
uint8_t *inboxStorage = nullptr;
DoorbellOutbox outbox;
DoorbellCommands commands;
Preferences storage;
char baseTopic[96], eventTopic[112], stateTopic[112], ackTopic[112], commandTopic[112], receiptTopic[112];
char will[160];
struct Inbound { bool receipt, retained; char body[512]; };

void deleteInbox() {
  if (inbox) { vQueueDelete(inbox); inbox = nullptr; }
  heap_caps_free(inboxStorage); inboxStorage = nullptr;
  inboxPsram = false;
}

void persist() {
  auto snapshot = outbox.snapshot();
  persistent = storage.putBytes("outbox", &snapshot, sizeof(snapshot)) == sizeof(snapshot);
  pending = outbox.count(); lost = outbox.lost();
}

void publishJson(const char *topic, cJSON *json, int qos = 1, bool retained = false) {
  if (!json) return;
  char *text = doorbellMqttJsonPrint(json);
  cJSON_Delete(json);
  if (text) {
    // Presence is a replaceable current value: don't retain stale heartbeats
    // in the QoS-1 retry outbox. Events and command ACKs still use QoS 1.
    int result = qos == 0 ? esp_mqtt_client_publish(client, topic, text, 0, 0, retained)
                          : esp_mqtt_client_enqueue(client, topic, text, 0, qos, retained, true);
    if (result < 0) ++publishFailures;
    cJSON_free(text);
  } else ++publishFailures;
}

void publishState() {
  cJSON *state = doorbellStatusJson();
  if (state) cJSON_AddBoolToObject(state, "online", true);
  publishJson(stateTopic, state, 0, true);
}

bool number(cJSON *json, const char *name, uint32_t &value) {
  cJSON *field = cJSON_GetObjectItemCaseSensitive(json, name);
  if (!cJSON_IsNumber(field) || !isfinite(field->valuedouble) || field->valuedouble < 0 ||
      field->valuedouble > 4294967295.0 || floor(field->valuedouble) != field->valuedouble) return false;
  value = static_cast<uint32_t>(field->valuedouble); return true;
}

bool bootNumber(const cJSON *field, uint32_t &value) {
  if (!cJSON_IsString(field) || strlen(field->valuestring) != 8) return false;
  for (size_t i = 0; i < 8; ++i) {
    char c = field->valuestring[i];
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
  }
  value = strtoul(field->valuestring, nullptr, 16); return true;
}

void receive(const Inbound &message) {
  cJSON *json = cJSON_Parse(message.body);
  if (!json) { ++rejected; return; }
  uint32_t boot = 0, id = 0, expiry = 0;
  if (message.receipt) {
    if (bootNumber(cJSON_GetObjectItemCaseSensitive(json, "boot_id"), boot) && number(json, "id", id) &&
        outbox.acknowledge(boot, id)) persist();
    cJSON_Delete(json); return;
  }
  cJSON *action = cJSON_GetObjectItemCaseSensitive(json, "action");
  cJSON *identifier = cJSON_GetObjectItemCaseSensitive(json, "command_id");
  bool validAction = cJSON_IsString(action) && (!strcmp(action->valuestring, "start") || !strcmp(action->valuestring, "stop"));
  if (!validAction || !cJSON_IsString(identifier) ||
      !bootNumber(cJSON_GetObjectItemCaseSensitive(json, "boot_id"), boot) ||
      !number(json, "valid_until_uptime_ms", expiry)) { cJSON_Delete(json); ++rejected; return; }
  auto result = message.retained ? DoorbellCommands::Expired : commands.accept(identifier->valuestring,
      strtoul(doorbellBootId(), nullptr, 16), boot, expiry, millis());
  bool ok = result == DoorbellCommands::Accepted || result == DoorbellCommands::Duplicate;
  if (result == DoorbellCommands::Accepted) doorbellSetManual(!strcmp(action->valuestring, "start"));
  if (!ok) ++rejected;
  cJSON *ack = cJSON_CreateObject();
  if (ack) {
    cJSON_AddStringToObject(ack, "command_id", identifier->valuestring);
    cJSON_AddStringToObject(ack, "device_id", doorbellDeviceId());
    cJSON_AddStringToObject(ack, "boot_id", doorbellBootId());
    cJSON_AddBoolToObject(ack, "ok", ok);
    cJSON_AddBoolToObject(ack, "duplicate", result == DoorbellCommands::Duplicate);
    if (!ok) cJSON_AddStringToObject(ack, "error", result == DoorbellCommands::WrongBoot ? "Device rebooted; retry from fresh state" : "Command expired, retained, or invalid");
    cJSON_AddItemToObject(ack, "state", doorbellStatusJson());
  }
  publishJson(ackTopic, ack);
  publishState();
  cJSON_Delete(json);
}

void mqttEvent(void *, esp_event_base_t, int32_t eventId, void *eventData) {
  auto *event = static_cast<esp_mqtt_event_handle_t>(eventData);
  if (eventId == MQTT_EVENT_BEFORE_CONNECT) {
    doorbellDiagnosticsMqttAttempt();
  } else if (eventId == MQTT_EVENT_CONNECTED) {
    connected = true;
    doorbellDiagnosticsMqttConnected();
    esp_mqtt_client_subscribe(client, commandTopic, 1);
    esp_mqtt_client_subscribe(client, receiptTopic, 1);
  } else if (eventId == MQTT_EVENT_DISCONNECTED) {
    connected = false;
    doorbellDiagnosticsMqttDisconnected();
  } else if (eventId == MQTT_EVENT_ERROR && event->error_handle) {
    const auto *error = event->error_handle;
    bool transport = error->error_type == MQTT_ERROR_TYPE_TCP_TRANSPORT;
    doorbellDiagnosticsMqttError(error->error_type,
        transport ? error->esp_tls_last_esp_err : 0,
        transport ? error->esp_tls_stack_err : 0,
        transport ? error->esp_tls_cert_verify_flags : 0,
        transport ? error->esp_transport_sock_errno : 0,
        error->error_type == MQTT_ERROR_TYPE_CONNECTION_REFUSED ? error->connect_return_code : 0);
  } else if (eventId == MQTT_EVENT_DATA) {
    if (event->current_data_offset || event->data_len != event->total_data_len || event->data_len <= 0 ||
        event->data_len >= static_cast<int>(sizeof(Inbound::body))) { ++rejected; return; }
    bool receipt = event->topic_len == static_cast<int>(strlen(receiptTopic)) && !strncmp(event->topic, receiptTopic, event->topic_len);
    bool command = event->topic_len == static_cast<int>(strlen(commandTopic)) && !strncmp(event->topic, commandTopic, event->topic_len);
    if (!receipt && !command) return;
    Inbound message = {};
    message.receipt = receipt; message.retained = event->retain;
    memcpy(message.body, event->data, event->data_len);
    if (xQueueSend(inbox, &message, 0) != pdPASS) ++rejected;
  }
}

void task(void *) {
  bool storageReady = storage.begin("doorbell-mqtt", false);
  if (storageReady && storage.getBytesLength("outbox") == sizeof(DoorbellOutbox::Snapshot)) {
    DoorbellOutbox::Snapshot saved = {};
    storage.getBytes("outbox", &saved, sizeof(saved));
    if (!outbox.restore(saved)) Serial.println("MQTT outbox checksum invalid; starting an empty outbox");
  }
  if (storageReady) persist();
  uint32_t after = 0, stateAt = 0, sentAt = 0, sentBoot = 0, sentId = 0;
  bool wasConnected = false;
  for (;;) {
    DoorbellLogic::Event event;
    bool modified = false;
    while (doorbellNextEvent(after, &event)) {
      outbox.append({static_cast<uint32_t>(strtoul(doorbellBootId(), nullptr, 16)), event.id,
          event.uptimeMs, static_cast<uint8_t>(event.type), {0, 0, 0}});
      after = event.id; modified = true;
    }
    if (modified && storageReady) persist();
    else { pending = outbox.count(); lost = outbox.lost(); }
    Inbound message;
    while (xQueueReceive(inbox, &message, 0) == pdPASS) receive(message);
    uint32_t now = millis();
    if (connected) {
      if (!wasConnected || uint32_t(now - stateAt) >= 5000) { publishState(); stateAt = now; }
      if (outbox.front() && (!wasConnected || outbox.front()->boot != sentBoot || outbox.front()->id != sentId || uint32_t(now - sentAt) >= 3000)) {
        auto record = *outbox.front();
        char boot[12]; snprintf(boot, sizeof(boot), "%08lx", (unsigned long)record.boot);
        cJSON *json = cJSON_CreateObject();
        if (json) {
          cJSON_AddStringToObject(json, "device_id", doorbellDeviceId());
          cJSON_AddStringToObject(json, "boot_id", boot);
          cJSON_AddNumberToObject(json, "id", record.id);
          cJSON_AddStringToObject(json, "type", record.type % 2 == 0 ? "motion" : "sound");
          cJSON_AddStringToObject(json, "phase", record.type >= 2 ? "cleared" : "triggered");
          cJSON_AddNumberToObject(json, "uptime_ms", record.uptimeMs);
        }
        publishJson(eventTopic, json); sentAt = now; sentBoot = record.boot; sentId = record.id;
      }
    }
    wasConnected = connected;
    vTaskDelay(pdMS_TO_TICKS(100));
  }
}
#endif
}  // namespace

void doorbellMqttStatus(cJSON *json) {
  cJSON_AddBoolToObject(json, "mqtt_configured", configured);
  cJSON_AddBoolToObject(json, "mqtt_connected", connected);
  cJSON_AddBoolToObject(json, "mqtt_outbox_persistent", persistent);
  cJSON_AddNumberToObject(json, "mqtt_pending_events", pending);
  cJSON_AddNumberToObject(json, "mqtt_events_lost", lost);
  cJSON_AddNumberToObject(json, "mqtt_messages_rejected", rejected);
  cJSON_AddNumberToObject(json, "mqtt_publish_failures", publishFailures);
  cJSON_AddBoolToObject(json, "mqtt_inbox_psram", inboxPsram);
  // Payload storage only; kernel queue bookkeeping is additional.
#if DOORBELL_HAS_MQTT
  cJSON_AddNumberToObject(json, "mqtt_inbox_storage_bytes", inbox ? 16 * sizeof(Inbound) : 0);
#endif
}

void doorbellMqttBegin() {
#if DOORBELL_HAS_MQTT
  if (configured || !DOORBELL_MQTT_HOST[0]) return;
  if (strcmp(doorbellDeviceId(), DOORBELL_MQTT_DEVICE_ID)) {
    Serial.println("MQTT identity does not match this board. Provision this board's device ID."); return;
  }
  snprintf(baseTopic, sizeof(baseTopic), "doorbell/%s", doorbellDeviceId());
  snprintf(eventTopic, sizeof(eventTopic), "%s/events", baseTopic);
  snprintf(stateTopic, sizeof(stateTopic), "%s/state", baseTopic);
  snprintf(ackTopic, sizeof(ackTopic), "%s/ack", baseTopic);
  snprintf(commandTopic, sizeof(commandTopic), "%s/commands", baseTopic);
  snprintf(receiptTopic, sizeof(receiptTopic), "%s/received", baseTopic);
  snprintf(will, sizeof(will), "{\"device_id\":\"%s\",\"boot_id\":\"%s\",\"online\":false}", doorbellDeviceId(), doorbellBootId());
  // Keep certificate time validation enabled even before a network time reply.
  time_t current = time(nullptr);
  if (current < DOORBELL_MQTT_BUILD_EPOCH) {
    timeval initial = {DOORBELL_MQTT_BUILD_EPOCH, 0}; settimeofday(&initial, nullptr);
  }
  configTime(0, 0, "pool.ntp.org", "time.cloudflare.com");
  // Neither producer nor consumer accesses this queue from an ISR. Keep the
  // same capacity. Kernel queue bookkeeping stays internal; only item storage
  // moves to PSRAM. No ISR or cache-disabled callback touches these items.
  inboxPsram = doorbellJsonUsesPsram();
  uint32_t queueCaps = MALLOC_CAP_8BIT | (inboxPsram ? MALLOC_CAP_SPIRAM : MALLOC_CAP_INTERNAL);
  inboxStorage = static_cast<uint8_t *>(heap_caps_malloc(16 * sizeof(Inbound), queueCaps));
  if (inboxStorage) inbox = xQueueCreateStatic(16, sizeof(Inbound), inboxStorage, &inboxControl);
  if (!inbox) { deleteInbox(); Serial.println("MQTT inbox allocation failed"); return; }
  esp_mqtt_client_config_t config = {};
  config.broker.address.hostname = DOORBELL_MQTT_HOST;
  config.broker.address.port = DOORBELL_MQTT_PORT;
  config.broker.address.transport = MQTT_TRANSPORT_OVER_SSL;
  config.broker.verification.certificate = DOORBELL_MQTT_CA;
  config.broker.verification.common_name = "doorbell-broker";
  config.credentials.client_id = doorbellDeviceId();
  config.credentials.authentication.certificate = DOORBELL_MQTT_CERT;
  config.credentials.authentication.key = DOORBELL_MQTT_KEY;
  config.session.keepalive = 15;
  config.session.last_will.topic = stateTopic;
  config.session.last_will.msg = will;
  config.session.last_will.qos = 1;
  config.session.last_will.retain = 1;
  config.network.reconnect_timeout_ms = 3000;
  config.network.timeout_ms = 3000;
  // Commands/receipts are bounded to 511 bytes. Larger outgoing status/ACK
  // packets are fragmented by ESP-MQTT; they do not require a 2-KiB I/O buffer.
  config.buffer.size = 1024;
  config.buffer.out_size = 1024;
  config.outbox.limit = 8192;
  client = esp_mqtt_client_init(&config);
  if (!client) { deleteInbox(); return; }
  esp_mqtt_client_register_event(client, MQTT_EVENT_ANY, mqttEvent, nullptr);
  if (esp_mqtt_client_start(client) != ESP_OK) {
    esp_mqtt_client_destroy(client); client = nullptr;
    deleteInbox(); return;
  }
  configured = xTaskCreate(task, "doorbell-mqtt", 6144, nullptr, 1, nullptr) == pdPASS;
  if (!configured) {
    esp_mqtt_client_stop(client); esp_mqtt_client_destroy(client); client = nullptr;
    deleteInbox();
    Serial.println("MQTT worker could not start");
  }
#endif
}
