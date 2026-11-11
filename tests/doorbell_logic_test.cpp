#include "../firmware/Doorbell/doorbell_logic.h"
#include <assert.h>
#include <iostream>

int main() {
  using Logic = DoorbellLogic;
  Logic state(30000, 60000, 2000, 3000);
  state.begin(0);
  assert(!state.liveActive(0));
  state.observe(100, true, false);
  state.observe(200, true, false);
  assert(state.newestId() == 0);  // PIR stabilization is ignored.
  state.observe(60000, true, false);
  assert(state.motionReady() && state.newestId() == 1);
  assert(state.liveActive(60000));
  state.observe(90000, true, false);  // Held HIGH keeps the window open.
  assert(state.newestId() == 1 && state.activityRemaining(90000) == 30000);
  state.observe(90001, false, false);
  state.observe(90101, false, false);
  state.observe(120001, false, false);
  assert(!state.liveActive(120001));

  state.setManual(true);
  assert(state.liveActive(130000));
  state.observe(130000, false, true);
  state.setManual(false);
  assert(state.liveActive(130001));  // Stop manual viewing preserves activity.
  const uint32_t soundId = state.newestId();
  state.observe(131000, false, true);
  assert(state.newestId() == soundId);  // Noise is rate-limited.
  state.observe(133000, false, true);
  assert(state.newestId() == soundId + 1);
  state.observe(163000, false, false);
  assert(!state.liveActive(163000));

  Logic earlySound(30000, 60000, 2000, 3000);
  earlySound.begin(1000);
  earlySound.observe(2999, false, true);
  assert(earlySound.newestId() == 0);
  earlySound.observe(3000, false, true);
  assert(earlySound.newestId() == 1);

  Logic wrapped(30000, 0, 0, 0);
  wrapped.begin(0xfffffff0U);
  wrapped.observe(0xfffffff0U, false, true);
  assert(wrapped.liveActive(0x10U));
  wrapped.observe(0x10U, false, false);
  assert(wrapped.activityRemaining(0x10U) == 30000 - 32);
  wrapped.observe(uint32_t(0xfffffff0U + 30000U), false, false);
  assert(!wrapped.liveActive(uint32_t(0xfffffff0U + 30000U)));

  Logic ring(30000, 0, 0, 0);
  ring.begin(0);
  for (uint32_t i = 1; i <= 30; ++i) ring.observe(i, false, true);
  assert(ring.oldestId() == 7 && ring.newestId() == 30 && ring.overwritten() == 6);
  Logic::Event events[Logic::capacity];
  assert(ring.copyAfter(0, events, Logic::capacity) == 24);
  assert(events[0].id == 7 && events[23].id == 30);
  assert(ring.copyAfter(29, events, Logic::capacity) == 1 && events[0].id == 30);
  assert(ring.copyAfter(30, events, Logic::capacity) == 0);
  assert(ring.copyAfter(0, events, 1) == 1 && events[0].id == 7);

  std::cout << "PASS: warm-up, sustained motion, expiry, manual/activity overlap, "
               "sound settling/cooldown, clock rollover, event retention and cursors\n";
}
