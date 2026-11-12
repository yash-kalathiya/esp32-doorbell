#pragma once
#include "esp_http_server.h"
#include "doorbell_logic.h"
#include <cJSON.h>

bool doorbellBegin();
void doorbellPoll();
bool doorbellLiveActive();
void doorbellStreamClient(bool opened);
void doorbellRegisterApi(httpd_handle_t server);
const char *doorbellDeviceId();
const char *doorbellBootId();
cJSON *doorbellStatusJson();
void doorbellSetManual(bool on);
bool doorbellNextEvent(uint32_t after, DoorbellLogic::Event *out);
esp_err_t doorbellCameraNightMode(bool enabled);
void doorbellCameraLight(bool enabled);
int doorbellCameraLightPercent();
