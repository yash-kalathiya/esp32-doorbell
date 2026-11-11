#pragma once
#include "esp_http_server.h"

bool doorbellBegin();
void doorbellPoll();
bool doorbellLiveActive();
void doorbellStreamClient(bool opened);
void doorbellRegisterApi(httpd_handle_t server);
