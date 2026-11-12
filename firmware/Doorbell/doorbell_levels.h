#pragma once
#include <stdint.h>

// Callers synchronize access. GPIO ISR updates use no allocation or logging.
class DoorbellLevel {
 public:
  struct Snapshot {
    bool initialized = false, high = false, completedHigh = false, completedLow = false;
    uint32_t sinceMs = 0, highPeriods = 0, lowPeriods = 0, lastHighMs = 0, lastLowMs = 0;
  };
  inline __attribute__((always_inline)) void observe(bool high, uint32_t now) {
    if (!state.initialized) {
      state.initialized = true; state.high = high; state.sinceMs = now;
      if (high) ++state.highPeriods; else ++state.lowPeriods;
    } else if (state.high != high) {
      if (state.high) { state.lastHighMs = uint32_t(now - state.sinceMs); state.completedHigh = true; }
      else { state.lastLowMs = uint32_t(now - state.sinceMs); state.completedLow = true; }
      state.high = high; state.sinceMs = now;
      if (high) ++state.highPeriods; else ++state.lowPeriods;
    }
  }
  Snapshot snapshot() const { return state; }
 private:
  Snapshot state;
};

struct cJSON;
cJSON *doorbellLevelJson(const DoorbellLevel::Snapshot &level, uint32_t now,
                        bool activeHigh, uint32_t event, uint32_t triggers);
