"""Synthetic rule validation; no claim of physical human detection accuracy."""
import sqlite3
import pytest
import httpx
from fastapi.testclient import TestClient
from server.security import SecurityMonitor
from server.app import create_app


class Clock:
    value = 100.
    def __call__(self): return self.value


def pilot(state='quiet', score=.01, identifier='initial'):
    return {'enabled':True, 'state':state, 'threshold':.04, 'clear_threshold':.02,
            'rate_hz':7, 'history':[{'at':identifier,'score':score}]}


def monitor(tmp_path):
    clock = Clock(); db = sqlite3.connect(tmp_path/'security.sqlite3')
    return SecurityMonitor(db, clock), clock


def window(m, clock, score=.1, online=True, **kwargs):
    clock.value += 1
    m.tick(pilot('elevated' if score>.04 else 'quiet',score,str(clock.value)),online,**kwargs)


def test_requires_real_calibrated_data_and_exit_delay(tmp_path):
    m,c=monitor(tmp_path)
    for bad in ({},pilot('no_data'),pilot('uncalibrated'),{**pilot(),'rate_hz':4}):
        with pytest.raises(ValueError): m.arm(bad,True)
    with pytest.raises(ValueError): m.arm(pilot(),False)
    m.arm(pilot(),True)
    for _ in range(14):window(m,c)
    assert not m.status(pilot(),True)['incidents']
    assert m.status(pilot(),True)['state']=='exit_delay'
    for _ in range(5):window(m,c)
    assert len(m.status(pilot(),True)['incidents'])==1


def test_one_incident_until_quiet_cooldown_ack_and_restart(tmp_path):
    m,c=monitor(tmp_path);m.arm(pilot(),True);c.value+=16
    context={'recent_camera_observations':[{'label':'Synthetic known observation'}],
             'identity_association':'unknown','sensor_policy':{'pir_stuck_high':True}}
    for _ in range(5):window(m,c,evidence_factory=lambda:context)
    first=m.status(pilot(),True)['incidents'][0]
    assert first['evidence']['context']==context
    # Even a known recent face does not suppress armed-away anonymous movement.
    for _ in range(100):window(m,c)
    assert len(m.status(pilot(),True)['incidents'])==1
    m.acknowledge(first['id']);m.acknowledge(first['id'])
    assert m.status(pilot(),True)['incidents'][0]['acknowledged_at']
    for _ in range(3):window(m,c,.01)
    for _ in range(5):window(m,c)
    assert len(m.status(pilot(),True)['incidents'])==2
    reboot=SecurityMonitor(m.db,c)
    assert not reboot.armed and len(reboot.status({},False)['incidents'])==2
    m.disarm()
    for _ in range(10):window(m,c)
    assert len(m.status({},False)['incidents'])==2


def test_missing_data_repeats_and_low_rate_never_complete_incident(tmp_path):
    m,c=monitor(tmp_path);m.arm(pilot(),True);c.value+=16
    for _ in range(4):window(m,c)
    same=pilot('elevated',.1,str(c.value))
    for _ in range(30):m.tick(same,True)
    assert not m.status(same,True)['incidents']
    m.tick(pilot('no_data'),True)
    assert m.high==0 and m.status(pilot('no_data'),True)['state']=='unavailable'
    for _ in range(4):window(m,c)
    c.value+=10;window(m,c)
    assert m.high==1 and not m.status(pilot(),True)['incidents']
    m.tick({**pilot(),'rate_hz':2},True)
    assert m.high==0
    m.tick(pilot(),False)
    assert m.status(pilot(),False)['state']=='unavailable'


def test_invalid_window_and_unavailable_context(tmp_path):
    m,c=monitor(tmp_path);m.arm(pilot(),True);c.value+=16
    for _ in range(4):window(m,c)
    window(m,c,float('nan'))
    assert not m.status(pilot(),True)['incidents']
    def unavailable():raise RuntimeError('context unavailable')
    for _ in range(5):window(m,c,evidence_factory=unavailable)
    assert m.status(pilot(),True)['incidents'][0]['evidence']['context']=={'context_unavailable':True}


def test_database_failure_is_visible_and_incident_retries_after_recovery(tmp_path):
    m,c=monitor(tmp_path);m.arm(pilot(),True);c.value+=16
    m.db.execute("CREATE TRIGGER deny_incident BEFORE INSERT ON security_incidents BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    for _ in range(5):window(m,c)
    state=m.status(pilot(),True)
    assert state['error'] and not state['incidents'] and not m.active_incident
    m.db.execute('DROP TRIGGER deny_incident')
    window(m,c)
    state=m.status(pilot(),True)
    assert not state['error'] and len(state['incidents'])==1 and m.active_incident


class NoFaces:
    def detect(self,jpeg): return {'faces':[],'rejected':0}


def test_api_starts_disarmed_guards_arm_and_same_origin(tmp_path):
    def offline(request):raise httpx.ConnectError('synthetic offline')
    with TestClient(create_app(tmp_path,httpx.MockTransport(offline),face_engine=NoFaces())) as client:
        assert client.get('/api/security').json()['state']=='disarmed'
        assert client.post('/api/security/arm').status_code==409
        assert client.post('/api/security/arm',headers={'Origin':'https://untrusted.example'}).status_code==403
        assert client.post('/api/security/disarm').status_code==200
        assert client.post('/api/security/unknown').status_code==404
        assert client.post('/api/security/incidents/unknown/acknowledge').status_code==404
