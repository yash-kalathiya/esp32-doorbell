#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>

// Bounded outbox persisted by the MQTT task, separate from the HTTP event ring.
class DoorbellOutbox {
 public:
  static constexpr size_t capacity = 24;
  struct Record { uint32_t boot, id, uptimeMs; uint8_t type; uint8_t reserved[3]; };
  struct Snapshot { uint32_t magic, count, lost; Record records[capacity]; uint32_t checksum; };
  DoorbellOutbox() { memset(&data, 0, sizeof(data)); data.magic = 0x44425131; }
  void append(Record record) {
    if (data.count == capacity) {
      memmove(data.records, data.records + 1, (capacity - 1) * sizeof(Record));
      --data.count; ++data.lost;
    }
    data.records[data.count++] = record;
  }
  bool acknowledge(uint32_t boot, uint32_t id) {
    if (!data.count || data.records[0].boot != boot || data.records[0].id != id) return false;
    memmove(data.records, data.records + 1, (--data.count) * sizeof(Record));
    memset(&data.records[data.count], 0, sizeof(Record));
    return true;
  }
  const Record *front() const { return data.count ? &data.records[0] : nullptr; }
  uint32_t count() const { return data.count; }
  uint32_t lost() const { return data.lost; }
  Snapshot snapshot() const { Snapshot out = data; out.checksum = hash(out); return out; }
  bool restore(const Snapshot &saved) {
    if (saved.magic != 0x44425131 || saved.count > capacity || saved.checksum != hash(saved)) return false;
    for (size_t i = 0; i < saved.count; ++i) if (saved.records[i].type > 3 || !saved.records[i].id) return false;
    data = saved; return true;
  }
 private:
  static uint32_t hash(const Snapshot &value) {
    const uint8_t *bytes = reinterpret_cast<const uint8_t *>(&value);
    uint32_t result = 2166136261u;
    for (size_t i = 0; i < offsetof(Snapshot, checksum); ++i) result = (result ^ bytes[i]) * 16777619u;
    return result;
  }
  Snapshot data;
};

class DoorbellCommands {
 public:
  enum Result { Accepted, Duplicate, Expired, WrongBoot, Invalid };
  Result accept(const char *id, uint32_t expectedBoot, uint32_t boot, uint32_t expires, uint32_t now) {
    if (!id || strlen(id) != 32) return Invalid;
    for (size_t i = 0; i < 32; ++i)
      if (!((id[i] >= '0' && id[i] <= '9') || (id[i] >= 'a' && id[i] <= 'f'))) return Invalid;
    if (boot != expectedBoot) return WrongBoot;
    for (const auto &entry : recent) if (!strcmp(entry, id)) return Duplicate;
    int32_t remaining = static_cast<int32_t>(expires - now);
    if (remaining < 0 || remaining > 10000) return Expired;
    strcpy(recent[next++ % 16], id);
    return Accepted;
  }
 private:
  char recent[16][33] = {};
  size_t next = 0;
};
