"""Experimental local CSI incident rules; no occupancy or identity inference."""
import json
import math
import sqlite3
import time
import uuid
from datetime import datetime, timezone


class SecurityMonitor:
    exit_seconds = 15
    elevated_windows = 5
    quiet_windows = 3
    cooldown_seconds = 60

    def __init__(self, db, clock=time.monotonic):
        self.db, self.clock = db, clock
        self.db.execute('''CREATE TABLE IF NOT EXISTS security_incidents (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL, acknowledged_at TEXT,
            evidence TEXT NOT NULL)''')
        self.db.commit()
        # Arm state deliberately does not survive a process restart.
        self.armed = False
        self.error = None
        self.high = self.low = 0
        self.active_incident = False
        self.exit_until = self.next_alert = 0
        self.last_point = self.last_sample = None

    @staticmethod
    def ready(pilot, online):
        return bool(online and pilot.get('enabled') and pilot.get('state') in ('quiet', 'elevated')
                    and pilot.get('threshold') is not None and pilot.get('rate_hz', 0) >= 5)

    def arm(self, pilot, online):
        if not self.ready(pilot, online):
            raise ValueError('Start CSI and complete an empty-room calibration before arming.')
        self.armed = True
        self.high = self.low = 0
        self.active_incident = False
        self.exit_until = self.clock() + self.exit_seconds
        self.next_alert = 0
        self.last_sample = None
        self.last_point = (pilot.get('history') or [{}])[-1].get('at')

    def disarm(self):
        self.armed = False
        self.high = self.low = 0
        self.active_incident = False
        self.last_sample = None

    def tick(self, pilot, online, evidence_factory=lambda: {}):
        if not self.armed:
            return
        current = self.clock()
        point = (pilot.get('history') or [{}])[-1]
        if current < self.exit_until or not self.ready(pilot, online):
            self.high = self.low = 0
            self.last_sample = None
            self.last_point = point.get('at')
            return
        if not point.get('at') or point['at'] == self.last_point:
            return  # Repeated SSE/status reads cannot manufacture valid windows.
        self.last_point = point['at']
        if self.last_sample is not None and current-self.last_sample > 2.5:
            self.high = self.low = 0
        self.last_sample = current
        score = point.get('score')
        if not isinstance(score, (float, int)) or not math.isfinite(score):
            self.high = self.low = 0
            return
        threshold = pilot['threshold']
        clear = pilot.get('clear_threshold')
        self.high = self.high + 1 if score > threshold else 0
        self.low = self.low + 1 if clear is not None and score < clear else 0
        if self.low >= self.quiet_windows:
            self.active_incident = False
        if self.high < self.elevated_windows or self.active_incident or current < self.next_alert:
            return
        try:
            context = evidence_factory()
        except Exception:
            context = {'context_unavailable': True}
        evidence = {'rule': 'sustained_csi_change', 'elevated_windows': self.high,
                    'threshold': threshold, 'latest_change': score,
                    'rate_hz': pilot['rate_hz'], 'context': context}
        try:
            with self.db:
                self.db.execute('INSERT INTO security_incidents VALUES (?,?,NULL,?)',
                    (uuid.uuid4().hex, datetime.now(timezone.utc).isoformat(), json.dumps(evidence)))
                self.db.execute('DELETE FROM security_incidents WHERE id NOT IN '
                    '(SELECT id FROM security_incidents ORDER BY created_at DESC LIMIT 100)')
        except sqlite3.Error:
            self.error = 'An incident could not be saved. Monitoring is degraded.'
            return
        self.error = None
        self.active_incident = True
        self.next_alert = current + self.cooldown_seconds

    def acknowledge(self, identifier):
        with self.db:
            cursor = self.db.execute('UPDATE security_incidents SET acknowledged_at=COALESCE(acknowledged_at,?) WHERE id=?',
                (datetime.now(timezone.utc).isoformat(), identifier))
            if not cursor.rowcount:
                raise KeyError('Incident not found')

    def status(self, pilot, online):
        if not self.armed: state = 'disarmed'
        elif not self.ready(pilot, online): state = 'unavailable'
        elif self.clock() < self.exit_until: state = 'exit_delay'
        else: state = 'watching'
        incidents = [dict(zip(('id','created_at','acknowledged_at','evidence'), row)) for row in
            self.db.execute('SELECT id,created_at,acknowledged_at,evidence FROM security_incidents ORDER BY created_at DESC LIMIT 20')]
        for item in incidents:
            item['evidence'] = json.loads(item['evidence'])
        return {'armed': self.armed, 'state': state, 'experimental': True,
                'ready': self.ready(pilot, online), 'error': self.error,
                'exit_remaining_s': max(0, int(self.exit_until-self.clock())) if self.armed else 0,
                'elevated_windows': self.high, 'required_windows': self.elevated_windows,
                'incidents': incidents}
