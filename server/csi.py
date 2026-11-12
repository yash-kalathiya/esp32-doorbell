"""Bounded, live-only CSI pilot. Signal activity is not occupancy or identity."""
import asyncio
from collections import deque
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import struct
import time
import uuid

import numpy as np

HEADER = struct.Struct('<IBBHI IbbBB'.replace(' ', ''))
MAGIC = 0xC5110001
MIN_HZ = 5


def decode_frame(data):
    if not 52 <= len(data) <= 276:
        raise ValueError('Invalid CSI datagram length')
    magic, node, antennas, count, frequency, sequence, rssi, noise, ppdu, flags = HEADER.unpack_from(data)
    if magic != MAGIC or node != 1 or antennas != 1 or not 16 <= count <= 128:
        raise ValueError('Unsupported CSI geometry')
    if len(data) != HEADER.size + count * 2 or frequency not in (*range(2412, 2473, 5), 2484):
        raise ValueError('Invalid CSI payload or frequency')
    if ppdu != 0 or flags & ~33:
        raise ValueError('Unsupported CSI flags')
    values = np.frombuffer(data, dtype=np.int8, offset=HEADER.size).astype(np.float32).reshape(-1, 2)
    # Always omit the first two pairs, even when the hardware prefix is valid.
    # That keeps geometry stable when first_word_invalid changes between frames.
    amplitude = np.hypot(values[2:, 0], values[2:, 1])
    energy = float(np.sqrt(np.mean(amplitude ** 2)))
    if not np.isfinite(energy) or energy < 1:
        raise ValueError('CSI has no usable amplitude')
    return {'sequence': sequence, 'signature': (frequency, count, flags & 1),
            'rssi': rssi, 'noise': noise, 'features': amplitude / energy}


class CSIPilot(asyncio.DatagramProtocol):
    def __init__(self, directory, clock=time.monotonic):
        self.directory = Path(directory) / 'csi'
        self.clock = clock
        self.transport = None
        self.port = None
        self.error = None
        self.enabled = False
        self.reason = 'Ready to start a new CSI session.'
        self.history = deque(maxlen=60)
        self.runs = deque(maxlen=6)
        self.context = {}
        if self.directory.exists():
            for path in sorted(self.directory.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:6]:
                try:
                    run = json.loads(path.read_text())
                    if run.get('id') == path.stem and len(path.stem) == 32:
                        self.add_video_modes(run)
                        self.runs.append(run)
                except (ValueError, OSError):
                    pass
        self.trial = None
        self.calibration = None
        self.calibration_id = None
        self.threshold = None
        self.clear_threshold = None
        self.calibration_error = None
        self.last_frame = None
        self.previous = None
        self.signature = None
        self.times = deque(maxlen=256)
        self.window = []
        self.window_started = self.clock()
        self.window_gap = False
        self.received = self.rejected = self.gaps = self.late = 0
        self.sequence = None
        self.rssi = None
        self.motion = None
        self.high_windows = self.low_windows = 0

    async def listen(self, port):
        try:
            loop = asyncio.get_running_loop()
            self.transport, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=('0.0.0.0', port))
            self.port = self.transport.get_extra_info('sockname')[1]
        except OSError as exc:
            self.error = f'CSI UDP listener unavailable: {exc.strerror or type(exc).__name__}'

    def activate(self, address, boot, seed):
        self.deactivate('Starting live capture.')
        self.enabled = True
        self.address, self.boot, self.seed = address, boot, seed
        self.started = self.clock()
        self.window_started = self.started
        self.received = self.rejected = self.gaps = self.late = 0
        self.sequence = None
        self.reason = 'Waiting for real CSI packets.'

    def deactivate(self, reason='Pilot stopped.'):
        self.finish_trial(False)
        self.enabled = False
        self.reason = reason
        self.last_frame = self.previous = self.signature = None
        self.motion = self.threshold = self.clear_threshold = self.calibration = None
        self.calibration_id = None
        self.calibration_error = None
        self.high_windows = self.low_windows = 0
        self.times.clear(); self.window.clear(); self.history.clear()
        self.window_gap = False

    def datagram_received(self, data, peer):
        if not self.enabled or peer[0] != self.address:
            self.rejected += 1
            return
        try:
            frame = decode_frame(data)
        except (ValueError, struct.error):
            self.rejected += 1
            return
        now = self.clock()
        sequence = frame['sequence']
        offset = (sequence - self.seed) & 0xffffffff
        if offset > (now - self.started) * 25 + 100:
            self.rejected += 1  # Different session or implausible forward jump.
            return
        if self.sequence is not None:
            delta = (sequence - self.sequence) & 0xffffffff
            if delta == 0 or delta >= 0x80000000:
                self.late += 1
                return
            self.gaps += delta - 1
        else:
            self.gaps += offset
        if self.signature is not None and frame['signature'] != self.signature:
            self.finish_trial(False)
            self.threshold = self.clear_threshold = self.calibration = None
            self.calibration_id = None
            self.calibration_error = 'Radio channel or frame geometry changed. Calibrate again.'
            self.previous = None
            self.window.clear()
        self.signature = frame['signature']
        self.flush(now)
        gap = self.last_frame is not None and now - self.last_frame > .5
        self.window_gap |= gap
        score = None
        if self.previous is not None and not gap:
            score = float(np.median(np.abs(frame['features'] - self.previous)))
        self.previous = frame['features']
        self.last_frame, self.sequence, self.rssi = now, sequence, frame['rssi']
        self.times.append(now); self.received += 1
        self.window.append(score)

    def rate(self):
        now = self.clock()
        recent = [at for at in self.times if now - at < 5]
        span = min(5, max(1, now - getattr(self, 'started', now)))
        return len(recent) / span

    def calibrate(self):
        if self.trial:
            raise ValueError('Finish the current recording before recalibrating.')
        if not self.fresh() or self.rate() < MIN_HZ:
            raise ValueError('Need fresh CSI at 5 packets/s or more before calibration.')
        self.threshold = self.clear_threshold = None
        self.calibration_error = None
        self.calibration = {'until': self.clock() + 60, 'scores': []}
        self.calibration_id = uuid.uuid4().hex
        self.window.clear(); self.window_started = self.clock(); self.window_gap = False
        self.previous = None
        self.motion = None; self.high_windows = self.low_windows = 0

    def fresh(self):
        return self.enabled and self.last_frame is not None and self.clock() - self.last_frame < 3

    def flush(self, now=None):
        now = self.clock() if now is None else now
        if now - self.window_started < 1:
            return
        values = [s for s in self.window if s is not None]
        valid = len(values) >= MIN_HZ and not self.window_gap and self.fresh() and self.rate() >= MIN_HZ
        score = float(np.median(values)) if valid else None
        point = {'at': datetime.now(timezone.utc).isoformat(), 'score': score, 'packets': len(self.window)}
        self.history.append(point)
        if self.calibration and score is not None:
            self.calibration['scores'].append(score)
        if self.calibration and now >= self.calibration['until']:
            scores = self.calibration['scores']
            self.calibration = None
            if len(scores) < 40:
                self.calibration_id = None
                self.calibration_error = 'Too few valid windows. Check packet rate and repeat empty-room calibration.'
            else:
                baseline = float(np.median(scores))
                mad = float(np.median(np.abs(np.asarray(scores) - baseline)))
                self.threshold = max(.015, baseline + max(.005, 6 * mad))
                self.clear_threshold = baseline + .4 * (self.threshold - baseline)
        if score is None or self.threshold is None:
            self.motion = None; self.high_windows = self.low_windows = 0
        else:
            self.high_windows = self.high_windows + 1 if score > self.threshold else 0
            self.low_windows = self.low_windows + 1 if score < self.clear_threshold else 0
            if self.high_windows >= 2: self.motion = True
            elif self.low_windows >= 3: self.motion = False
        if self.trial:
            self.trial['writer'].writerow([point['at'], self.trial['label'], point['packets'],
                '' if score is None else score, self.rate(), self.rssi, self.gaps,
                '' if self.threshold is None else self.threshold,
                '' if self.motion is None else int(self.motion),
                self.context.get('video_receiving'), self.context.get('mqtt_online'),
                self.context.get('internal_free_bytes'), self.context.get('internal_min_free_bytes'),
                self.context.get('stream_errors'), self.context.get('wifi_disconnects'),
                self.context.get('queue_drops'), self.context.get('send_errors')])
            self.trial['file'].flush()
            if score is not None: self.trial['scores'].append(score)
            if now >= self.trial['until']: self.finish_trial(True)
        self.window.clear(); self.window_started = now; self.window_gap = False

    def begin_trial(self, label):
        if label not in ('empty', 'walking'):
            raise ValueError('Choose empty or walking.')
        if not self.fresh(): raise ValueError('No fresh CSI is arriving.')
        if self.calibration: raise ValueError('Finish calibration before starting a recording.')
        if self.trial: raise ValueError('A recording is already running.')
        self.directory.mkdir(parents=True, exist_ok=True)
        if sum(1 for _ in self.directory.glob('*.csv')) >= 100:
            raise ValueError('Recording limit reached. Export and remove old pilot files first.')
        self.window.clear(); self.window_started = self.clock(); self.window_gap = False
        self.previous = None
        identifier = uuid.uuid4().hex
        handle = (self.directory / f'{identifier}.csv').open('w', newline='')
        writer = csv.writer(handle)
        writer.writerow(['utc', 'label', 'packets', 'normalized_change', 'packets_per_second', 'rssi_dbm',
                         'sequence_gaps', 'threshold', 'elevated_signal', 'video_receiving', 'mqtt_online', 'internal_free_bytes',
                         'internal_min_free_bytes', 'stream_errors', 'wifi_disconnects', 'queue_drops', 'send_errors'])
        self.trial = {'id': identifier, 'label': label, 'until': self.clock()+30,
                      'file': handle, 'writer': writer, 'scores': [], 'context': dict(self.context),
                      'calibration_id': self.calibration_id, 'session_seed': getattr(self, 'seed', None),
                      'boot_id': getattr(self, 'boot', None), 'radio_geometry': self.signature}

    def finish_trial(self, completed):
        if not self.trial: return
        trial, self.trial = self.trial, None
        trial['file'].close()
        scores = trial['scores']
        summary = {'id': trial['id'], 'label': trial['label'], 'completed': completed,
                   'boot_id': trial['boot_id'], 'radio_geometry': trial['radio_geometry'],
                   'calibration_id': trial['calibration_id'], 'session_seed': trial['session_seed'],
                   'context_start': trial['context'], 'context_end': dict(self.context),
                   'valid_windows': len(scores), 'mean_change': float(np.mean(scores)) if scores else None}
        self.add_video_modes(summary)
        self.runs.appendleft(summary)
        (self.directory / f"{trial['id']}.json").write_text(json.dumps(summary))

    def add_video_modes(self, summary):
        if 'video_modes' in summary: return
        try:
            with (self.directory / (summary['id'] + '.csv')).open(newline='') as handle:
                summary['video_modes'] = sorted({row['video_receiving'] for row in csv.DictReader(handle)})
        except (OSError, KeyError):
            summary['video_modes'] = []

    def comparison(self):
        if not self.runs: return None
        newest = self.runs[0]
        def scope(run):
            return run.get('boot_id'), run.get('session_seed'), run.get('calibration_id')
        eligible = [r for r in self.runs if scope(r) == scope(newest) and not r.get('exclude_reason')]
        walking = [r for r in eligible if r['label'] == 'walking' and r['completed'] and r['valid_windows'] >= 20]
        empty = [r for r in eligible if r['label'] == 'empty' and r['completed'] and r['valid_windows'] >= 20]
        for walk in walking:
            for quiet in empty:
                modes = walk.get('video_modes', [])
                if (len(modes) != 1 or modes[0] not in ('True', 'False') or modes != quiet.get('video_modes')
                    or walk.get('boot_id') != quiet.get('boot_id') or walk.get('radio_geometry') != quiet.get('radio_geometry')
                    or walk.get('session_seed') != quiet.get('session_seed')
                    or walk.get('calibration_id') != quiet.get('calibration_id')):
                    continue
                low, high = quiet['mean_change'], walk['mean_change']
                if low is None or high is None: continue
                return {'empty_mean': low, 'walking_mean': high, 'ratio': high/low if low > 0 else None,
                        'video_running': modes[0] == 'True', 'empty_id': quiet['id'], 'walking_id': walk['id']}
        return None

    def status(self):
        fresh = self.fresh()
        if not self.enabled: state = 'stopped'
        elif not fresh: state = 'no_data'
        elif self.rate() < MIN_HZ: state = 'low_rate'
        elif self.calibration: state = 'calibrating'
        elif self.threshold is None: state = 'uncalibrated'
        elif self.motion is None: state = 'waiting'
        else: state = 'elevated' if self.motion else 'quiet'
        return {'enabled': self.enabled, 'state': state, 'error': self.error,
                'reason': self.reason, 'port': self.port, 'packets': self.received, 'rejected': self.rejected,
                'sequence_gaps': self.gaps, 'late_packets': self.late, 'rate_hz': round(self.rate(), 1) if fresh else 0,
                'rssi': self.rssi if fresh else None, 'threshold': self.threshold, 'clear_threshold': self.clear_threshold,
                'comparison': self.comparison(),
                'calibration_error': self.calibration_error,
                'calibration_id': self.calibration_id,
                'calibration_remaining_s': max(0, int(self.calibration['until']-self.clock())) if self.calibration else None,
                'history': list(self.history), 'runs': list(self.runs),
                'trial': {'label': self.trial['label'], 'remaining_s': max(0, int(self.trial['until']-self.clock()))} if self.trial else None}

    def close(self):
        self.deactivate('Server stopped.')
        if self.transport: self.transport.close()
