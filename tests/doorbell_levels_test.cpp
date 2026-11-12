#include <cassert>
#include "../firmware/Doorbell/doorbell_levels.h"
#include "../firmware/Doorbell/doorbell_logic.h"
#include "../firmware/Doorbell/doorbell_delivery.h"

int main() {
  DoorbellLevel level;
  level.observe(true, 100); level.observe(true, 150);
  assert(level.snapshot().sinceMs == 100 && level.snapshot().highPeriods == 1);
  level.observe(false, 180);
  assert(level.snapshot().lastHighMs == 80 && level.snapshot().completedHigh);
  level.observe(true, 205);
  assert(level.snapshot().lastLowMs == 25 && level.snapshot().highPeriods == 2);
  DoorbellLevel wrap; wrap.observe(true, 0xfffffff0U); wrap.observe(false, 0x10U);
  assert(wrap.snapshot().lastHighMs == 32);

  using L = DoorbellLogic;
  L logic(30000, 0, 0, 3000); logic.begin(0);
  logic.observe(100, true, L::SoundPulse{}); logic.observe(200, true, L::SoundPulse{});
  assert(logic.motionTriggerCount() == 1 && logic.motionActiveEvent() == 1);
  logic.observe(500, true, L::SoundPulse{});
  assert(logic.newestId() == 1);
  logic.observe(600, false, L::SoundPulse{}); logic.observe(700, false, L::SoundPulse{});
  assert(logic.newestId() == 2 && logic.motionActiveEvent() == 0);
  logic.observe(800, true, L::SoundPulse{}); logic.observe(900, true, L::SoundPulse{});
  assert(logic.motionTriggerCount() == 2 && logic.motionActiveEvent() == 3);
  // Pulse completed between 20-ms samples: preserve actual edge timestamps.
  logic.observe(1000, true, L::SoundPulse{true, true, 992, 994});
  assert(logic.soundTriggerCount() == 1 && logic.soundActiveEvent() == 0);
  L::Event events[L::capacity]; auto count = logic.copyAfter(0, events, L::capacity);
  assert(count == 5 && events[3].type == L::EventType::Sound && events[3].uptimeMs == 992);
  assert(events[4].type == L::EventType::SoundCleared && events[4].uptimeMs == 994);
  logic.observe(1100, true, L::SoundPulse{true, true, 1090, 1098});
  assert(logic.newestId() == 5); // Cooldown suppresses the whole pair.
  logic.observe(4000, true, L::SoundPulse{true, false, 3999, 0});
  assert(logic.soundTriggerCount() == 2 && logic.soundActiveEvent() == 6);
  logic.observe(4100, true, L::SoundPulse{false, true, 3999, 4090});
  assert(logic.newestId() == 7 && logic.soundActiveEvent() == 0);
  logic.observe(4120, true, L::SoundPulse{}); assert(logic.newestId() == 7);
  // NVS record layout stays unchanged; both legacy starts and new ends restore.
  static_assert(sizeof(DoorbellOutbox::Record) == 16, "Outbox migration required");
  DoorbellOutbox outbox;
  outbox.append({1,1,100,0,{0,0,0}}); outbox.append({1,2,200,2,{0,0,0}});
  DoorbellOutbox reboot; assert(reboot.restore(outbox.snapshot()) && reboot.count() == 2);
}
