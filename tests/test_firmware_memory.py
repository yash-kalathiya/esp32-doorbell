"""Native allocation-routing checks. Host byte counts are not ESP32 free-heap measurements."""
import ctypes.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def memory_check(tmp_path_factory):
    compiler = shutil.which("g++")
    library = ctypes.util.find_library("cjson")
    include = Path(os.environ.get("ESP32_JSON_INCLUDE", str(Path.home() /
        ".arduino15/packages/esp32/tools/esp32-libs/3.3.12/include/json/cJSON")))
    if not compiler or not library or not (include / "cJSON.h").is_file():
        pytest.skip("Native memory check needs g++, libcjson and the ESP32 SDK cJSON header")
    directory = tmp_path_factory.mktemp("firmware-memory")
    (directory / "Arduino.h").write_text("""
#pragma once
#include <cstdint>
extern bool psramAvailable;
inline bool psramFound() { return psramAvailable; }
""")
    (directory / "esp_heap_caps.h").write_text("""
#pragma once
#include <cstdint>
#include <cstddef>
constexpr uint32_t MALLOC_CAP_INTERNAL=1, MALLOC_CAP_8BIT=2, MALLOC_CAP_SPIRAM=4;
void *heap_caps_malloc(size_t bytes, uint32_t caps);
void heap_caps_free(void *p);
""")
    harness = directory / "check.cpp"
    harness.write_text(r'''
#include <algorithm>
#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>
#include <esp_heap_caps.h>
#include "doorbell_memory.h"
bool psramAvailable=true, failPsram=false;
std::mutex lock;
struct Allocation { size_t bytes; int heap; };
std::unordered_map<void *,Allocation> live;
size_t used[2]={}, peak[2]={}, peakTotal=0;
void *heap_caps_malloc(size_t bytes,uint32_t caps) {
  std::lock_guard<std::mutex> guard(lock);
  int heap=(caps&MALLOC_CAP_SPIRAM)?1:0;
  if (heap && failPsram) return nullptr;
  void *p=std::malloc(bytes); if (!p) return nullptr;
  live.emplace(p,Allocation{bytes,heap}); used[heap]+=bytes;
  peak[heap]=std::max(peak[heap],used[heap]);
  peakTotal=std::max(peakTotal,used[0]+used[1]); return p;
}
void heap_caps_free(void *p) {
  if (!p) return;
  std::lock_guard<std::mutex> guard(lock);
  auto i=live.find(p); assert(i!=live.end());
  used[i->second.heap]-=i->second.bytes;live.erase(i);std::free(p);
}
void *baselineAllocate(size_t n) {
  // Match the installed SDK's 4096-byte preference threshold, excluding
  // allocator headers/fragmentation. Every JSON node remains a host-size node.
  return heap_caps_malloc(n,MALLOC_CAP_8BIT|(n>4096?MALLOC_CAP_SPIRAM:MALLOC_CAP_INTERNAL));
}
cJSON *status() {
  cJSON *j=cJSON_CreateObject();assert(j);
  cJSON_AddStringToObject(j,"device_id","test-device");
  cJSON_AddStringToObject(j,"boot_id","1234abcd");
  cJSON_AddNumberToObject(j,"uptime_ms",4294967295.0);
  auto *d=cJSON_AddObjectToObject(j,"diagnostics");
  for(int i=0;i<70;i++) {
    char name[32]; std::snprintf(name,sizeof(name),"diagnostic_value_%d",i);
    cJSON_AddNumberToObject(d,name,i);
  }
  return j;
}
void workload(bool optimized) {
  std::vector<cJSON *> trees;
  std::vector<char *> output;
  for(int i=0;i<3;i++) {
    cJSON *j=status();trees.push_back(j);
    // Same 4-KiB buffer on both sides isolates placement from realloc effects.
    char *text=optimized?doorbellMqttJsonPrint(j):static_cast<char *>(cJSON_malloc(4096));
    assert(text);
    if(!optimized) assert(cJSON_PrintPreallocated(j,text,4096,false));
    assert(std::strstr(text,"diagnostic_value_69"));output.push_back(text);
  }
  for(auto *j:trees)cJSON_Delete(j);
  for(auto *text:output)cJSON_free(text);
  assert(live.empty() && used[0]==0 && used[1]==0);
}
int main(int argc,char **) {
  if(argc>1) {
    psramAvailable=false;doorbellMemoryBegin(); assert(!doorbellJsonUsesPsram());
    workload(true); assert(peak[0]>0 && peak[1]==0);
    return 0;
  }
  cJSON_Hooks baseline={baselineAllocate,heap_caps_free};cJSON_InitHooks(&baseline);
  workload(false);size_t before=peak[0],totalBefore=peakTotal;
  peak[0]=peak[1]=peakTotal=0;
  doorbellMemoryBegin();assert(doorbellJsonUsesPsram());
  // A repeated initializer must not change hooks while documents are alive.
  auto *existing=status();psramAvailable=false;doorbellMemoryBegin();
  assert(doorbellJsonUsesPsram());cJSON_Delete(existing);
  peak[0]=peak[1]=peakTotal=0;
  workload(true);size_t after=peak[0],totalAfter=peakTotal;
  assert(before>0 && after==0 && totalBefore==totalAfter);
  std::vector<std::thread> workers;
  for(int i=0;i<4;i++)workers.emplace_back([] {
    for(int n=0;n<500;n++) {
      auto *j=status();auto *text=doorbellMqttJsonPrint(j);assert(text);
      cJSON_Delete(j);cJSON_free(text);
    }
  });
  for(auto &thread:workers)thread.join();
  assert(live.empty());
  failPsram=true;assert(cJSON_CreateObject()==nullptr);
  assert(peak[0]==0);failPsram=false;
  auto *large=cJSON_CreateObject();std::string big(5000,'x');
  cJSON_AddStringToObject(large,"oversize",big.c_str());
  assert(doorbellMqttJsonPrint(large)==nullptr);cJSON_Delete(large);
  auto *memory=cJSON_CreateObject();doorbellMemoryStatus(memory);
  assert(cJSON_GetObjectItemCaseSensitive(memory,"json_allocation_failures")->valueint>=1);
  assert(cJSON_GetObjectItemCaseSensitive(memory,"json_print_failures")->valueint==1);
  cJSON_Delete(memory);assert(live.empty());
  std::cout<<"{\"baseline_internal_peak_bytes\":"<<before
           <<",\"optimized_internal_peak_bytes\":"<<after
           <<",\"baseline_total_peak_bytes\":"<<totalBefore
           <<",\"optimized_total_peak_bytes\":"<<totalAfter
           <<",\"concurrent_cycles\":2000,\"live_allocations_after\":"<<live.size()<<"}\n";
}
''')
    binary = directory / "check"
    subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pthread",
        f"-I{directory}", f"-I{include}", f"-I{ROOT / 'firmware/Doorbell'}",
        str(harness), str(ROOT / "firmware/Doorbell/doorbell_memory.cpp"),
        f"-l:{library}", "-o", str(binary)], check=True, capture_output=True, text=True)
    return binary


def test_psram_routing_and_concurrent_cleanup(memory_check):
    result = subprocess.run([str(memory_check)], check=True, capture_output=True, text=True)
    values = json.loads(result.stdout)
    assert values["optimized_internal_peak_bytes"] == 0
    assert values["optimized_total_peak_bytes"] == values["baseline_total_peak_bytes"]
    assert values["live_allocations_after"] == 0
    print("Native controlled allocator comparison:", json.dumps(values))


def test_no_psram_fallback(memory_check):
    subprocess.run([str(memory_check), "no-psram"], check=True, capture_output=True, text=True)
