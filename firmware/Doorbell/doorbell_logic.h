#pragma once
#include <stdint.h>
#include <stddef.h>

// Hardware-independent state. Callers provide synchronization on the ESP32.
class DoorbellLogic {
 public:
  enum class EventType : uint8_t { Motion, Sound, MotionCleared, SoundCleared };
  struct SoundPulse { bool started = false, ended = false; uint32_t startedAt = 0, endedAt = 0; };
  struct Event { uint32_t id; uint32_t uptimeMs; EventType type; };
  static constexpr size_t capacity = 24;

  DoorbellLogic(uint32_t hold, uint32_t warmup, uint32_t settle, uint32_t cooldown)
      : holdMs(hold), warmupMs(warmup), settleMs(settle), cooldownMs(cooldown) {}

  void begin(uint32_t now) { startedAt = now; }
  void observe(uint32_t now, bool pir, bool soundPulse) {
    observeInput(now, pir, soundPulse, now, nullptr);
  }
  void observe(uint32_t now, bool pir, const SoundPulse &pulse) {
    observeInput(now, pir, pulse.started, pulse.startedAt, &pulse);
  }

 private:
  void observeInput(uint32_t now, bool pir, bool soundPulse, uint32_t pulseAt, const SoundPulse *edges) {
    if (!pirReady && uint32_t(now - startedAt) >= warmupMs) pirReady = true;
    if (!soundReady && uint32_t(now - startedAt) >= settleMs) soundReady = true;
    if (pir != pirCandidate) { pirCandidate = pir; candidateSince = now; }
    if (uint32_t(now - candidateSince) >= 100) pirStable = pirCandidate;
    const bool motion = pirReady && pirStable;
    if (motion) {
      activitySeen = true;
      lastActivity = now;
      if (!previousMotion) { append(now, EventType::Motion); motionEvent = newestId(); }
    }
    if (!motion && previousMotion) { append(now, EventType::MotionCleared); motionEvent = 0; }
    previousMotion = motion;
    if (soundReady && soundPulse) {
      activitySeen = true;
      lastActivity = now;
      if (!soundSeen || uint32_t(now - lastSoundEvent) >= cooldownMs) {
        append(pulseAt, EventType::Sound);
        if (edges) soundEvent = newestId();
        soundSeen = true;
        lastSoundEvent = now;
      }
    }
    if (edges && edges->ended && soundEvent) {
      append(edges->endedAt, EventType::SoundCleared); soundEvent = 0;
    }
    if (activitySeen && uint32_t(now - lastActivity) >= holdMs) activitySeen = false;
  }

 public:
  uint32_t motionTriggerCount() const { return motionTriggers; }
  uint32_t soundTriggerCount() const { return soundTriggers; }
  uint32_t motionActiveEvent() const { return motionEvent; }
  uint32_t soundActiveEvent() const { return soundEvent; }
  void setManual(bool on) { manual = on; }
  bool manualRequested() const { return manual; }
  bool activityActive(uint32_t now) const {
    return activitySeen && uint32_t(now - lastActivity) < holdMs;
  }
  bool liveActive(uint32_t now) const { return manual || activityActive(now); }
  uint32_t activityRemaining(uint32_t now) const {
    return activityActive(now) ? holdMs - uint32_t(now - lastActivity) : 0;
  }
  bool motionReady() const { return pirReady; }
  bool stableMotion() const { return pirReady && pirStable; }
  uint32_t newestId() const { return nextId - 1; }
  uint32_t oldestId() const { return count ? events[head].id : nextId; }
  uint32_t overwritten() const { return overflow; }

  size_t copyAfter(uint32_t after, Event *out, size_t limit) const {
    size_t copied = 0;
    for (size_t i = 0; i < count && copied < limit; ++i) {
      const Event &event = events[(head + i) % capacity];
      if (event.id > after) out[copied++] = event;
    }
    return copied;
  }

 private:
  void append(uint32_t now, EventType type) {
    if (type == EventType::Motion) ++motionTriggers;
    if (type == EventType::Sound) ++soundTriggers;
    size_t index = (head + count) % capacity;
    if (count == capacity) { head = (head + 1) % capacity; ++overflow; }
    else ++count;
    events[index] = {nextId++, now, type};
  }
  uint32_t holdMs, warmupMs, settleMs, cooldownMs;
  uint32_t startedAt = 0, lastActivity = 0, lastSoundEvent = 0, candidateSince = 0;
  bool manual = false, activitySeen = false, soundSeen = false;
  bool pirReady = false, soundReady = false, pirCandidate = false;
  bool pirStable = false, previousMotion = false;
  Event events[capacity] = {};
  size_t head = 0, count = 0;
  uint32_t nextId = 1, overflow = 0;
  uint32_t motionTriggers = 0, soundTriggers = 0, motionEvent = 0, soundEvent = 0;
};
