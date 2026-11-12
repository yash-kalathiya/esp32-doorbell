#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>

// RuView ADR-018 framing. Payload is ESP-IDF imaginary/real signed byte pairs.
namespace DoorbellCsiWire {
constexpr size_t headerBytes = 20, maxIqBytes = 256;
constexpr uint32_t magic = 0xC5110001;
inline void put16(uint8_t *p, uint16_t n) { p[0] = n; p[1] = n >> 8; }
inline void put32(uint8_t *p, uint32_t n) { for (int i=0; i<4; ++i) p[i] = n >> (8*i); }
inline size_t encode(uint8_t *out, const int8_t *iq, size_t length, uint8_t channel,
                     uint32_t sequence, int8_t rssi, int8_t noise, bool invalid, bool wide) {
  if (!out || !iq || length < 32 || length > maxIqBytes || length % 2 || channel < 1 || channel > 14) return 0;
  memset(out, 0, headerBytes);
  put32(out, magic); out[4] = 1; out[5] = 1;
  put16(out+6, length/2);
  put32(out+8, channel == 14 ? 2484 : 2407 + channel*5);
  put32(out+12, sequence); out[16] = uint8_t(rssi); out[17] = uint8_t(noise);
  out[19] = (invalid ? 32 : 0) | (wide ? 1 : 0);
  memcpy(out+headerBytes, iq, length);
  if (invalid) memset(out+headerBytes, 0, 4);
  return headerBytes + length;
}
}
