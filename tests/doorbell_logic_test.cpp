#include "../firmware/Doorbell/doorbell_logic.h"
#include <cassert>
#include <iostream>

int main() {
  using L = DoorbellLogic;
  L pir(30000, 60000, 2000, 3000); pir.begin(0);
  pir.observe(100, true, false); pir.observe(200, true, false);
  assert(pir.newestId() == 0);
  pir.observe(60000, true, false);
  assert(pir.motionReady() && pir.motionTriggerCount() == 1 && pir.liveActive(60000));
  pir.observe(90000, true, false);
  assert(!pir.liveActive(90000) && pir.stableMotion() && !pir.pirStuck());
  pir.observe(120000, true, false);
  assert(pir.pirStuck() && pir.stableMotion() && pir.motionActiveEvent() == 1);
  assert(pir.motionTriggerCount() == 1 && !pir.liveActive(120000));
  // A healthy sound input can activate while a faulty PIR stays HIGH.
  pir.observe(120001, true, true);
  assert(pir.soundTriggerCount() == 1 && pir.liveActive(120001));
  pir.observe(150002, true, false);
  assert(!pir.liveActive(150002) && pir.pirStuck());
  pir.observe(150003, false, false); pir.observe(150103, false, false);
  assert(!pir.stableMotion() && !pir.pirStuck());
  pir.observe(150200, true, false); pir.observe(150300, true, false);
  assert(pir.motionTriggerCount() == 2 && pir.liveActive(150300));

  L sound(30000, 60000, 2000, 3000); sound.begin(0);
  sound.observe(1999, false, true); sound.observe(2000, false, true);
  assert(sound.soundTriggerCount() == 0);
  sound.observe(4500, false, false); sound.observe(4501, false, true);
  assert(sound.soundTriggerCount() == 1);
  for (uint32_t t=4510; t<=40000; t+=10) sound.observe(t, false, true);
  assert(sound.soundTriggerCount() == 1 && sound.suppressedSoundCount() > 3000);
  assert(!sound.liveActive(40000)); // Noise cannot refresh the deadline.
  sound.observe(42001, false, false);
  assert(sound.soundActiveEvent() == 0 && sound.autoArmed());
  sound.observe(42002, false, true);
  assert(sound.soundTriggerCount() == 2 && sound.liveActive(42002));
  sound.setManual(true); sound.observe(100000, false, false);
  assert(sound.liveActive(100000)); sound.setManual(false);
  assert(!sound.liveActive(100000));

  L stuck(30000, 0, 0, 3000); stuck.begin(0);
  stuck.observe(2001, false, L::SoundPulse{true,false,2001,0,true,true});
  stuck.observe(62001, false, L::SoundPulse{false,false,0,0,true,true});
  assert(stuck.soundNoisy() && stuck.soundTriggerCount() == 1 && !stuck.liveActive(62001));
  stuck.observe(62002, false, L::SoundPulse{false,true,0,62002,true,false});
  stuck.observe(64002, false, L::SoundPulse{false,false,0,0,true,false});
  assert(!stuck.soundNoisy() && stuck.soundActiveEvent() == 0);
  L bootNoise(30000,0,2000,3000); bootNoise.begin(0);
  bootNoise.observe(1,false,L::SoundPulse{true,false,1,0,true,true});
  bootNoise.observe(60001,false,L::SoundPulse{false,false,0,0,true,true});
  assert(bootNoise.soundNoisy() && bootNoise.soundTriggerCount()==0);

  L wrapped(30000,0,0,0); uint32_t start=0xfffff000U; wrapped.begin(start);
  uint32_t pulse=start+3000; wrapped.observe(pulse,false,true);
  assert(wrapped.liveActive(uint32_t(pulse+32)));
  wrapped.observe(uint32_t(pulse+30000),false,false);
  assert(!wrapped.liveActive(uint32_t(pulse+30000)));

  L ring(30000,0,0,0,0); ring.begin(0);
  for (uint32_t i=1;i<=30;++i) ring.observe(i,false,true);
  assert(ring.oldestId()==37 && ring.newestId()==60 && ring.overwritten()==36);
  L::Event events[L::capacity]; assert(ring.copyAfter(0,events,L::capacity)==24);
  assert(events[0].id==37 && events[23].id==60);
  assert(ring.copyAfter(59,events,L::capacity)==1 && events[0].id==60);
  assert(ring.copyAfter(60,events,L::capacity)==0);
  std::cout << "PASS: bounded automatic video, quiet rearm, sound incidents, fault isolation, manual view, rollover and retention\n";
}
