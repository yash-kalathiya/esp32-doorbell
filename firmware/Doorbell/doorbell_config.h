#pragma once

// These pins require the microSD slot to remain unused.
constexpr bool DOORBELL_PIR_ENABLED = true;
constexpr int DOORBELL_PIR_PIN = 13;
constexpr bool DOORBELL_SOUND_ENABLED = true;
constexpr int DOORBELL_SOUND_PIN = 14;
// Most LM393 sound modules assert D0 LOW. Change if your module is opposite.
constexpr bool DOORBELL_SOUND_ACTIVE_LOW = true;

constexpr unsigned long DOORBELL_ACTIVITY_MS = 30000;
constexpr unsigned long DOORBELL_PIR_WARMUP_MS = 60000;
constexpr unsigned long DOORBELL_SOUND_SETTLE_MS = 2000;
constexpr unsigned long DOORBELL_SOUND_COOLDOWN_MS = 3000;
constexpr unsigned long DOORBELL_CALLBACK_RETRY_MS = 5000;

// Optional laptop endpoint, e.g. http://192.168.1.20:8000/api/events.
// Empty disables pushing. Polling /api/events works without a laptop server.
// It can also be set later through POST /api/config and saved across reboots.
constexpr const char *DOORBELL_DEFAULT_EVENT_URL = "";
