#pragma once
#include <cJSON.h>
#include "esp_err.h"

// Disabled at every boot. Runtime settings are deliberately not persisted.
esp_err_t doorbellCsiConfigure(bool enabled, const char *host, unsigned port);
void doorbellCsiPoll();
void doorbellCsiStatus(cJSON *root);
