#pragma once

#include <cJSON.h>
#include <stdint.h>

void doorbellDiagnosticsBegin();
void doorbellDiagnosticsStatus(cJSON *json);
void doorbellDiagnosticsMqttAttempt();
void doorbellDiagnosticsMqttConnected();
void doorbellDiagnosticsMqttDisconnected();
void doorbellDiagnosticsMqttError(int type, int espError, int tlsError,
                                 int verifyFlags, int socketError, int brokerCode);
void doorbellDiagnosticsCaptureBegin();
void doorbellDiagnosticsCaptureEnd(bool success, uint32_t elapsedMs);
void doorbellDiagnosticsStream(bool success);
