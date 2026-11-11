#include <cassert>
#include <cstring>
#include "../firmware/Doorbell/doorbell_delivery.h"

int main() {
  DoorbellOutbox queue;
  for (uint32_t id = 1; id <= 27; ++id) queue.append({1, id, id * 100, 0, {0, 0, 0}});
  assert(queue.count() == 24 && queue.lost() == 3 && queue.front()->id == 4);
  assert(!queue.acknowledge(2, 4));
  assert(!queue.acknowledge(1, 5));
  assert(queue.acknowledge(1, 4) && queue.front()->id == 5);
  auto saved = queue.snapshot();
  DoorbellOutbox rebooted;
  assert(rebooted.restore(saved));
  assert(rebooted.count() == 23 && rebooted.front()->id == 5 && rebooted.lost() == 3);
  saved.records[0].id = 999;
  assert(!rebooted.restore(saved));
  DoorbellCommands commands;
  const char *id = "0123456789abcdef0123456789abcdef";
  assert(commands.accept(id, 1, 2, 1000, 0) == DoorbellCommands::WrongBoot);
  assert(commands.accept(id, 1, 1, 500, 1000) == DoorbellCommands::Expired);
  assert(commands.accept(id, 1, 1, 20000, 0) == DoorbellCommands::Expired);
  assert(commands.accept("invalid", 1, 1, 2000, 1000) == DoorbellCommands::Invalid);
  assert(commands.accept(id, 1, 1, 2000, 1000) == DoorbellCommands::Accepted);
  assert(commands.accept(id, 1, 1, 2000, 1100) == DoorbellCommands::Duplicate);
  DoorbellCommands wrap;
  assert(wrap.accept(id, 1, 1, 1000, 0xfffffff0u) == DoorbellCommands::Accepted);
  assert(wrap.accept("1123456789abcdef0123456789abcdef", 1, 1, 0xfffffff0u, 1000) == DoorbellCommands::Expired);
}
