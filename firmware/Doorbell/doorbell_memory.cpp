#include <Arduino.h>
#include <esp_heap_caps.h>
#include <atomic>
#include "doorbell_memory.h"

namespace {
bool initialized = false, usePsram = false;
std::atomic<uint32_t> allocationFailures{0}, printFailures{0};
constexpr int MQTT_JSON_BYTES = 4096;

void *jsonAllocate(size_t bytes) {
  const uint32_t caps = MALLOC_CAP_8BIT | (usePsram ? MALLOC_CAP_SPIRAM : MALLOC_CAP_INTERNAL);
  void *result = heap_caps_malloc(bytes, caps);
  // Once PSRAM is selected, do not consume TLS's internal heap if PSRAM fills.
  if (!result) allocationFailures.fetch_add(1, std::memory_order_relaxed);
  return result;
}
}  // namespace

void doorbellMemoryBegin() {
  if (initialized) return;
  usePsram = psramFound();
  cJSON_Hooks hooks = {jsonAllocate, heap_caps_free};
  cJSON_InitHooks(&hooks);
  initialized = true;
}

bool doorbellJsonUsesPsram() { return usePsram; }

void doorbellMemoryStatus(cJSON *memory) {
  if (!memory) return;
  cJSON_AddBoolToObject(memory, "json_psram", usePsram);
  cJSON_AddNumberToObject(memory, "json_allocation_failures", allocationFailures.load(std::memory_order_relaxed));
  cJSON_AddNumberToObject(memory, "json_print_failures", printFailures.load(std::memory_order_relaxed));
}

char *doorbellMqttJsonPrint(const cJSON *json) {
  // One bounded allocation rather than repeated allocate/copy/grow/shrink.
  char *text = static_cast<char *>(cJSON_malloc(MQTT_JSON_BYTES));
  if (!text) return nullptr;
  if (!cJSON_PrintPreallocated(const_cast<cJSON *>(json), text, MQTT_JSON_BYTES, false)) {
    cJSON_free(text);
    printFailures.fetch_add(1, std::memory_order_relaxed);
    return nullptr;
  }
  return text;
}
