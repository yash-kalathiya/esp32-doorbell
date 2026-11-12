#pragma once

#include <cJSON.h>

// Install once in setup(), before any networking task can create JSON objects.
void doorbellMemoryBegin();
bool doorbellJsonUsesPsram();
void doorbellMemoryStatus(cJSON *memory);
// Caller owns the returned buffer and releases it using cJSON_free().
char *doorbellMqttJsonPrint(const cJSON *json);
