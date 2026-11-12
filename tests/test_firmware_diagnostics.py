"""Exercise the real firmware serializer with host hardware stubs and real cJSON."""
import ctypes.util
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_diagnostics_survive_reconnect_and_fit_mqtt(tmp_path):
    compiler = shutil.which("g++")
    library = ctypes.util.find_library("cjson")
    include = Path(os.environ.get("ESP32_JSON_INCLUDE", str(Path.home() /
        ".arduino15/packages/esp32/tools/esp32-libs/3.3.12/include/json/cJSON")))
    if not compiler or not library or not (include / "cJSON.h").is_file():
        pytest.skip("Native firmware diagnostic check needs g++, libcjson and the ESP32 SDK cJSON header")
    headers = {
        "Arduino.h": """
#pragma once
#include <cstdint>
extern uint32_t clockMs;
inline uint32_t millis() { return clockMs; }
inline bool psramFound() { return true; }
struct Console { template<class... Args> void printf(const char *, Args...) {} };
inline Console Serial;
using portMUX_TYPE = int;
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(mux) ((void)(mux))
#define portEXIT_CRITICAL(mux) ((void)(mux))
""",
        "WiFi.h": """
#pragma once
#include <functional>
enum arduino_event_id_t { ARDUINO_EVENT_WIFI_STA_GOT_IP, ARDUINO_EVENT_WIFI_STA_DISCONNECTED };
struct arduino_event_info_t { struct { int reason; } wifi_sta_disconnected; };
struct Wifi { std::function<void(arduino_event_id_t, arduino_event_info_t)> callback;
  template<class F> void onEvent(F f) { callback=f; }
};
inline Wifi WiFi;
""",
        "esp_heap_caps.h": """
#pragma once
#include <cstdint>
#include <cstdlib>
constexpr uint32_t MALLOC_CAP_INTERNAL=1, MALLOC_CAP_8BIT=2, MALLOC_CAP_SPIRAM=4;
extern uint32_t available, largest;
inline void *heap_caps_malloc(size_t size, uint32_t) { return std::malloc(size); }
inline void heap_caps_free(void *p) { std::free(p); }
inline uint32_t heap_caps_get_free_size(uint32_t caps) { return caps==MALLOC_CAP_SPIRAM ? 3000000 : available; }
inline uint32_t heap_caps_get_largest_free_block(uint32_t) { return largest; }
inline uint32_t heap_caps_get_minimum_free_size(uint32_t) { return 1500; }
""",
        "esp_system.h": """
#pragma once
enum esp_reset_reason_t { ESP_RST_POWERON, ESP_RST_EXT, ESP_RST_SW, ESP_RST_PANIC,
  ESP_RST_INT_WDT, ESP_RST_TASK_WDT, ESP_RST_WDT, ESP_RST_DEEPSLEEP, ESP_RST_BROWNOUT, ESP_RST_SDIO };
inline esp_reset_reason_t esp_reset_reason() { return ESP_RST_TASK_WDT; }
""",
    }
    for name, body in headers.items():
        (tmp_path / name).write_text(body)
    harness = tmp_path / "check.cpp"
    harness.write_text(r'''
#include <cassert>
#include <climits>
#include <cstring>
#include <string>
#include <WiFi.h>
#include "doorbell_diagnostics.h"
#include "doorbell_memory.h"
#include "doorbell_levels.h"
uint32_t clockMs=0, available=4000, largest=2500;
cJSON *at(cJSON *j, const char *key) { auto *v=cJSON_GetObjectItemCaseSensitive(j,key); assert(v); return v; }
int main() {
  doorbellMemoryBegin();
  doorbellDiagnosticsBegin();
  cJSON *initial=cJSON_CreateObject(); doorbellDiagnosticsStatus(initial);
  assert(!strcmp(at(initial,"firmware_version")->valuestring,"sensor-time-1"));
  assert(cJSON_IsNull(at(at(at(initial,"diagnostics"),"mqtt"),"last_error")));
  cJSON_Delete(initial);
  clockMs=100;
  doorbellDiagnosticsMqttAttempt(); doorbellDiagnosticsMqttError(1,32769,-9984,8,104,0);
  clockMs=200; available=60000; largest=32000;
  doorbellDiagnosticsMqttDisconnected(); doorbellDiagnosticsMqttConnected();
  arduino_event_info_t info={}; info.wifi_sta_disconnected.reason=201;
  WiFi.callback(ARDUINO_EVENT_WIFI_STA_DISCONNECTED,info);
  WiFi.callback(ARDUINO_EVENT_WIFI_STA_GOT_IP,info);
  doorbellDiagnosticsCaptureBegin(); doorbellDiagnosticsCaptureBegin();
  doorbellDiagnosticsCaptureEnd(true,24); doorbellDiagnosticsStream(true);
  cJSON *j=cJSON_CreateObject(); doorbellDiagnosticsStatus(j);
  auto *d=at(j,"diagnostics"), *mqtt=at(d,"mqtt"), *error=at(mqtt,"last_error");
  assert(!strcmp(at(d,"reset_name")->valuestring,"task_watchdog"));
  assert(!strcmp(at(mqtt,"last_event")->valuestring,"connected"));
  assert(at(mqtt,"errors")->valueint==1 && at(mqtt,"connections")->valueint==1);
  assert(at(error,"uptime_ms")->valueint==100);
  assert(at(error,"tls_error")->valueint==-9984 && at(error,"certificate_flags")->valueint==8);
  assert(at(error,"internal_free_bytes")->valueint==4000);
  assert(at(at(d,"memory"),"internal_free_bytes")->valueint==60000);
  assert(at(at(d,"wifi"),"last_disconnect_reason")->valueint==201);
  assert(at(at(d,"wifi"),"disconnects")->valueint==1);
  assert(at(at(d,"camera"),"captures_in_flight")->valueint==1);
  cJSON_Delete(j);
  doorbellDiagnosticsCaptureEnd(false,4000); doorbellDiagnosticsStream(false);
  clockMs=UINT32_MAX; available=UINT32_MAX; largest=UINT32_MAX;
  doorbellDiagnosticsMqttError(INT_MAX,INT_MIN,INT_MIN,INT_MAX,INT_MAX,INT_MAX);
  j=cJSON_CreateObject(); doorbellDiagnosticsStatus(j); d=at(j,"diagnostics");
  doorbellMemoryStatus(at(d,"memory"));
  assert(cJSON_IsTrue(at(at(d,"memory"),"json_psram")));
  assert(at(at(d,"camera"),"capture_failures")->valueint==1);
  assert(at(at(d,"camera"),"captures_in_flight")->valueint==0);
  assert(at(at(d,"camera"),"max_capture_duration_ms")->valueint==4000);
  char *text=doorbellMqttJsonPrint(j); assert(text);
  // Keep at least 2 KiB of the broker/gateway's 4 KiB limit for existing status or ACK fields.
  assert(strlen(text)<2048);
  cJSON *decoded=cJSON_Parse(text); assert(decoded);
  assert(at(at(at(decoded,"diagnostics"),"mqtt"),"last_error"));
  cJSON_Delete(decoded); cJSON_free(text); cJSON_Delete(j);
  // Conservative whole-status budget: actual diagnostic and GPIO serializers
  // plus 1,400 bytes reserved for the remaining camera/MQTT fields.
  j=cJSON_CreateObject();doorbellDiagnosticsStatus(j);
  doorbellMemoryStatus(at(at(j,"diagnostics"),"memory"));
  std::string base(1400,'x');cJSON_AddStringToObject(j,"base_status_budget",base.c_str());
  auto *levels=cJSON_AddObjectToObject(j,"sensor_levels");
  DoorbellLevel::Snapshot level;level.initialized=level.high=level.completedHigh=level.completedLow=true;
  level.highPeriods=level.lowPeriods=level.lastHighMs=level.lastLowMs=UINT32_MAX;
  cJSON_AddItemToObject(levels,"motion",doorbellLevelJson(level,UINT32_MAX,true,UINT32_MAX,UINT32_MAX));
  cJSON_AddItemToObject(levels,"sound",doorbellLevelJson(level,UINT32_MAX,false,UINT32_MAX,UINT32_MAX));
  assert(at(at(levels,"sound"),"active_level")->valueint==0);
  text=doorbellMqttJsonPrint(j);assert(text && strlen(text)<4096);
  cJSON_free(text);cJSON_Delete(j);
}
''')
    binary = tmp_path / "check"
    subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
        f"-I{tmp_path}", f"-I{include}", f"-I{ROOT / 'firmware/Doorbell'}",
        str(harness), str(ROOT / "firmware/Doorbell/doorbell_diagnostics.cpp"),
        str(ROOT / "firmware/Doorbell/doorbell_memory.cpp"),
        str(ROOT / "firmware/Doorbell/doorbell_levels.cpp"),
        f"-l:{library}", "-o", str(binary)], check=True, capture_output=True, text=True)
    subprocess.run([str(binary)], check=True, capture_output=True, text=True)
