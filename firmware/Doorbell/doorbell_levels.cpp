#include <cJSON.h>
#include "doorbell_levels.h"

cJSON *doorbellLevelJson(const DoorbellLevel::Snapshot &level, uint32_t now,
                        bool activeHigh, uint32_t event, uint32_t triggers) {
  cJSON *json = cJSON_CreateObject();
  if (!json) return nullptr;
  cJSON_AddNumberToObject(json, "level", level.high ? 1 : 0);
  cJSON_AddNumberToObject(json, "active_level", activeHigh ? 1 : 0);
  cJSON_AddNumberToObject(json, "level_for_ms", level.initialized ? uint32_t(now - level.sinceMs) : 0);
  cJSON_AddNumberToObject(json, "high_periods", level.highPeriods);
  cJSON_AddNumberToObject(json, "low_periods", level.lowPeriods);
  if (level.completedHigh) cJSON_AddNumberToObject(json, "last_high_ms", level.lastHighMs);
  else cJSON_AddNullToObject(json, "last_high_ms");
  if (level.completedLow) cJSON_AddNumberToObject(json, "last_low_ms", level.lastLowMs);
  else cJSON_AddNullToObject(json, "last_low_ms");
  cJSON_AddNumberToObject(json, "active_event_id", event);
  cJSON_AddNumberToObject(json, "trigger_count", triggers);
  return json;
}
