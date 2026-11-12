#pragma once
#include <stdint.h>
#include <stddef.h>

// Hardware-independent state. Callers provide synchronization on the ESP32.
class DoorbellLogic {
 public:
  enum class EventType : uint8_t { Motion, Sound, MotionCleared, SoundCleared };
  struct SoundPulse {
    bool started = false, ended = false; uint32_t startedAt = 0, endedAt = 0;
    bool sampled = false, active = false;
  };
  struct Event { uint32_t id; uint32_t uptimeMs; EventType type; };
  static constexpr size_t capacity = 24;

  DoorbellLogic(uint32_t hold, uint32_t warmup, uint32_t settle, uint32_t cooldown,
                uint32_t quiet = 2000, uint32_t fault = 60000)
      : holdMs(hold), warmupMs(warmup), settleMs(settle), cooldownMs(cooldown),
        quietMs(quiet), faultMs(fault) {}

  void begin(uint32_t now) { startedAt = lastSoundActivity = now; }
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
    if (motion && !previousMotion) {
      motionBeganAt = now;
      append(now, EventType::Motion); motionEvent = newestId();
      requestActivity(now);
    }
    if (!motion && previousMotion) { append(now, EventType::MotionCleared); motionEvent = 0; }
    pirFault = motion && uint32_t(now - motionBeganAt) >= faultMs;
    previousMotion = motion;
    // The quiet interval groups a burst of comparator edges into one incident.
    const bool wasQuiet = !soundInputActive && uint32_t(now-lastSoundActivity) >= quietMs;
    if (edges && edges->sampled) soundInputActive = edges->active;
    else if (edges) {
      if (edges->started) soundInputActive = true;
      if (edges->ended) soundInputActive = false;
    } else soundInputActive = false;  // A boolean pulse is instantaneous.
    if (soundPulse || soundInputActive) {
      if (!soundNoiseSeen) { soundBeganAt = now; soundNoiseSeen = true; }
      lastSoundActivity = now;
    }
    if (soundReady && soundPulse) {
      if (!soundEvent && wasQuiet && (!soundSeen || uint32_t(now - lastSoundEvent) >= cooldownMs)) {
        append(pulseAt, EventType::Sound);
        soundEvent = newestId();
        soundSeen = true;
        lastSoundEvent = now;
        requestActivity(now);
      } else ++soundSuppressed;
    }
    if (!soundInputActive && uint32_t(now-lastSoundActivity) >= quietMs) {
      if (soundEvent) { append(now, EventType::SoundCleared); soundEvent = 0; }
      soundNoiseSeen = false;
    }
    soundFault = soundNoiseSeen && uint32_t(now-soundBeganAt) >= faultMs;
    if (activitySeen && uint32_t(now - lastActivity) >= holdMs) activitySeen = false;
    // A continuously HIGH PIR is still displayed truthfully, but after its
    // fault timeout it cannot prevent a healthy sound input from rearming.
    if (!activitySeen && (!motion || pirFault) && !soundInputActive && !soundEvent &&
        uint32_t(now-lastSoundActivity) >= quietMs) automaticArmed = true;
  }

  void requestActivity(uint32_t now) {
    if (!automaticArmed || activityActive(now)) return;
    automaticArmed = false; activitySeen = true; lastActivity = now;
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
  bool pirStuck() const { return pirFault; }
  bool soundNoisy() const { return soundFault; }
  uint32_t suppressedSoundCount() const { return soundSuppressed; }
  bool autoArmed() const { return automaticArmed; }
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
  uint32_t holdMs, warmupMs, settleMs, cooldownMs, quietMs, faultMs;
  uint32_t startedAt = 0, lastActivity = 0, lastSoundEvent = 0, candidateSince = 0;
  uint32_t lastSoundActivity = 0, motionBeganAt = 0, soundBeganAt = 0, soundSuppressed = 0;
  bool automaticArmed = true, soundInputActive = false, pirFault = false, soundFault = false;
  bool soundNoiseSeen = false;
  bool manual = false, activitySeen = false, soundSeen = false;
  bool pirReady = false, soundReady = false, pirCandidate = false;
  bool pirStable = false, previousMotion = false;
  Event events[capacity] = {};
  size_t head = 0, count = 0;
  uint32_t nextId = 1, overflow = 0;
  uint32_t motionTriggers = 0, soundTriggers = 0, motionEvent = 0, soundEvent = 0;
};
