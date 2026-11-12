"""Synthetic protocol/analysis checks. These do not establish physical CSI accuracy."""
import csv
from pathlib import Path
import shutil
import struct
import subprocess

from fastapi.testclient import TestClient
import httpx
import numpy as np
import pytest

from server.app import create_app
from server.csi import CSIPilot, HEADER, MAGIC, decode_frame


ADDRESS = '192.168.12.140'


class Clock:
    value = 100.
    def __call__(self): return self.value


def packet(sequence=0, movement=False, count=64, frequency=2412, flags=0):
    iq = np.zeros((count, 2), dtype=np.int8)
    iq[:, 0] = 20
    if movement == 'noise':
        iq[:count//2, 0] = 21
    elif movement:
        iq[:count//2, 0], iq[count//2:, 0] = 8, 50
    return HEADER.pack(MAGIC, 1, 1, count, frequency, sequence & 0xffffffff, -50, -96, 0, flags) + iq.tobytes()


def pilot(tmp_path, seed=0):
    clock = Clock()
    receiver = CSIPilot(tmp_path, clock)
    receiver.activate(ADDRESS, 'synthetic-boot', seed)
    return receiver, clock


def feed(receiver, clock, seconds, moving=False):
    sequence = receiver.seed if receiver.sequence is None else (receiver.sequence + 1) & 0xffffffff
    for i in range(round(seconds * 20)):
        clock.value += .05
        receiver.datagram_received(packet(sequence+i, movement=moving if i % 2 == 0 else False), (ADDRESS, 1234))
    receiver.flush()


def test_strict_decoder_and_invalid_prefix():
    assert HEADER.size == 20
    assert decode_frame(packet())['features'].shape == (62,)
    sanitized = bytearray(packet(flags=32))
    sanitized[20:24] = b'\x7f' * 4
    assert np.allclose(decode_frame(sanitized)['features'], 1)
    for data in (b'', packet()+b'x', b'wrong'+packet()[5:], packet(count=129), packet(frequency=5000), packet(flags=4)):
        with pytest.raises(ValueError): decode_frame(data)


def test_peer_filter_sequence_gaps_rollover_and_staleness(tmp_path):
    receiver, clock = pilot(tmp_path, 0xfffffffe)
    receiver.datagram_received(packet(0xfffffffe), ('192.168.12.99', 1234))
    assert receiver.received == 0 and receiver.rejected == 1
    for sequence in (0xfffffffe, 0xffffffff, 1, 1, 0):
        clock.value += .05
        receiver.datagram_received(packet(sequence), (ADDRESS, 1234))
    assert receiver.received == 3 and receiver.gaps == 1 and receiver.late == 2
    clock.value += 4
    receiver.flush()
    assert receiver.status()['state'] == 'no_data'
    assert receiver.status()['rate_hz'] == 0


def test_calibration_hysteresis_and_radio_change(tmp_path):
    receiver, clock = pilot(tmp_path)
    feed(receiver, clock, 2)
    receiver.calibrate()
    feed(receiver, clock, 61)
    assert receiver.threshold == pytest.approx(.015)
    feed(receiver, clock, 4)
    assert receiver.status()['state'] == 'quiet'
    feed(receiver, clock, 4, moving=True)
    assert receiver.status()['state'] == 'elevated'
    feed(receiver, clock, 5)
    assert receiver.status()['state'] == 'quiet'
    clock.value += .05
    receiver.datagram_received(packet(receiver.sequence+1, frequency=2437), (ADDRESS, 1234))
    assert receiver.threshold is None
    assert receiver.status()['state'] == 'uncalibrated'
    assert 'changed' in receiver.status()['calibration_error']


def test_positive_noise_baseline_can_clear_motion(tmp_path):
    receiver, clock = pilot(tmp_path)
    feed(receiver, clock, 2, moving='noise')
    receiver.calibrate()
    feed(receiver, clock, 61, moving='noise')
    assert receiver.clear_threshold > .02
    feed(receiver, clock, 5, moving='noise')
    assert receiver.status()['state'] == 'quiet'
    feed(receiver, clock, 4, moving=True)
    assert receiver.status()['state'] == 'elevated'
    feed(receiver, clock, 5, moving='noise')
    assert receiver.status()['state'] == 'quiet'


def test_missing_data_never_completes_calibration_or_proves_quiet(tmp_path):
    receiver, clock = pilot(tmp_path)
    with pytest.raises(ValueError): receiver.calibrate()
    feed(receiver, clock, 2)
    receiver.calibrate()
    clock.value += 65
    receiver.flush()
    assert receiver.threshold is None
    assert receiver.status()['state'] == 'no_data'
    assert 'Too few' in receiver.status()['calibration_error']


def test_labelled_csv_completed_interrupted_and_reload(tmp_path):
    receiver, clock = pilot(tmp_path)
    feed(receiver, clock, 2)
    receiver.begin_trial('empty')
    feed(receiver, clock, 31)
    assert receiver.trial is None and receiver.runs[0]['completed']
    assert receiver.runs[0]['valid_windows'] >= 25
    csv_path = receiver.directory / (receiver.runs[0]['id'] + '.csv')
    rows = list(csv.DictReader(csv_path.open()))
    assert rows and all(row['label'] == 'empty' for row in rows)
    assert 'internal_free_bytes' in rows[0] and 'video_receiving' in rows[0]
    receiver.begin_trial('walking')
    feed(receiver, clock, 2, moving=True)
    receiver.deactivate()
    assert not receiver.runs[0]['completed']
    restored = CSIPilot(tmp_path)
    assert len(restored.runs) == 2
    assert restored.status()['state'] == 'stopped'


def test_comparison_requires_matching_video_conditions(tmp_path):
    receiver, clock = pilot(tmp_path)
    feed(receiver, clock, 2, moving='noise')
    receiver.context = {'video_receiving': True}
    receiver.begin_trial('empty'); feed(receiver, clock, 31, moving='noise')
    receiver.context = {'video_receiving': False}
    receiver.begin_trial('walking'); feed(receiver, clock, 31, moving=True)
    assert receiver.comparison() is None
    receiver.begin_trial('empty'); feed(receiver, clock, 31, moving='noise')
    comparison = receiver.comparison()
    assert comparison and not comparison['video_running'] and comparison['ratio'] > 1


def test_comparison_rejects_different_calibration_and_geometry_interrupts_recording(tmp_path):
    receiver, clock = pilot(tmp_path)
    feed(receiver, clock, 2)
    receiver.context = {'video_receiving': False}
    receiver.calibrate(); feed(receiver, clock, 61)
    receiver.begin_trial('empty'); feed(receiver, clock, 31)
    first = receiver.runs[0]['calibration_id']
    receiver.calibrate(); feed(receiver, clock, 61)
    receiver.begin_trial('walking'); feed(receiver, clock, 31, moving=True)
    assert receiver.runs[0]['calibration_id'] != first
    assert receiver.comparison() is None
    receiver.begin_trial('empty'); feed(receiver, clock, 31)
    assert receiver.comparison() is not None
    receiver.begin_trial('walking'); feed(receiver, clock, 2, moving=True)
    clock.value += .05
    receiver.datagram_received(packet(receiver.sequence+1, frequency=2437), (ADDRESS, 1234))
    assert receiver.trial is None and not receiver.runs[0]['completed']
    assert receiver.runs[0]['radio_geometry'][0] == 2412


def test_excluded_or_older_session_comparisons_are_not_presented_as_latest(tmp_path):
    receiver, clock = pilot(tmp_path)
    feed(receiver, clock, 2)
    receiver.context = {'video_receiving': False}
    receiver.begin_trial('empty'); feed(receiver, clock, 31)
    receiver.begin_trial('walking'); feed(receiver, clock, 31, moving=True)
    assert receiver.comparison()
    receiver.runs[0]['exclude_reason'] = 'Receiver moved during the trial.'
    assert receiver.comparison() is None
    receiver.runs[0].pop('exclude_reason')
    receiver.activate(ADDRESS, 'another-boot', 5000); feed(receiver, clock, 2)
    receiver.begin_trial('empty'); feed(receiver, clock, 31)
    assert receiver.comparison() is None


def test_calibration_and_recording_cannot_overlap(tmp_path):
    receiver, clock = pilot(tmp_path); feed(receiver, clock, 2)
    receiver.calibrate()
    with pytest.raises(ValueError, match='Finish calibration'):
        receiver.begin_trial('empty')
    feed(receiver, clock, 61)
    receiver.begin_trial('empty')
    with pytest.raises(ValueError, match='current recording'):
        receiver.calibrate()


class NoFaces:
    def detect(self, jpeg): return {'faces': [], 'rejected': 0}


def test_api_activation_ack_calibration_guard_stop_and_safe_downloads(tmp_path):
    commands = []
    def camera(request):
        if request.url.path == '/api/csi/config':
            import json
            body = json.loads(request.content)
            commands.append(body)
            return httpx.Response(200, json={'device_id': 'synthetic-camera', 'boot_id':'synthetic-boot',
                'csi_pilot': {'enabled':body['enabled'], 'supported':True, 'sequence_start':123}})
        if request.url.path == '/api/status':
            return httpx.Response(200, json={'device_id':'synthetic-camera', 'boot_id':'synthetic-boot',
                'csi_pilot': {'supported':True, 'enabled':False}})
        if request.url.path == '/api/events':
            return httpx.Response(200, json={'boot_id':'synthetic-boot', 'next_after':0, 'events':[]})
        return httpx.Response(404)
    with TestClient(create_app(tmp_path, httpx.MockTransport(camera), face_engine=NoFaces(), csi_port=0)) as client:
        response = client.post('/api/csi/start')
        assert response.status_code == 200, response.text
        assert response.json()['enabled'] and response.json()['state'] == 'no_data'
        assert commands[0]['port'] > 0 and commands[0]['host']
        assert client.post('/api/csi/calibrate').status_code == 409
        assert client.post('/api/csi/record-walking').status_code == 409
        assert client.post('/api/csi/start', headers={'Origin':'http://evil.example'}).status_code == 403
        assert client.get('/api/csi/recordings/not-a-run').status_code == 404
        assert client.post('/api/csi/stop').status_code == 200
        assert not commands[-1]['enabled']


def test_old_firmware_has_no_fake_pilot_data(tmp_path):
    with TestClient(create_app(tmp_path, httpx.MockTransport(lambda _: httpx.Response(404)), face_engine=NoFaces())) as client:
        assert client.post('/api/csi/start').status_code == 409
        status = client.get('/api/csi').json()
        assert not status['enabled'] and status['packets'] == 0 and not status['history']
        html = client.get('/').text
        assert 'recognition-tools' not in html and 'Use device camera' not in html


def test_real_firmware_encoder_is_adr018_compatible(tmp_path):
    compiler = shutil.which('g++')
    if not compiler: pytest.skip('Native encoder check needs g++')
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / 'encoder.cpp'
    source.write_text('''#include <cstdio>
#include "doorbell_csi_wire.h"
int main() {
  int8_t iq[128]; for (int i=0;i<128;++i) iq[i]=20;
  uint8_t out[276];
  size_t n=DoorbellCsiWire::encode(out,iq,128,1,123,-50,-96,true,false);
  if(n!=148 || out[19]!=32) return 1;
  for(int i=20;i<24;++i) if(out[i]) return 2;
  fwrite(out,1,n,stdout);
}''')
    executable = tmp_path / 'encoder'
    subprocess.run([compiler,'-std=c++17','-Wall','-Wextra','-Werror',f'-I{root / "firmware/Doorbell"}',str(source),'-o',str(executable)],check=True)
    decoded = decode_frame(subprocess.check_output([str(executable)]))
    assert decoded['sequence'] == 123 and decoded['rssi'] == -50
