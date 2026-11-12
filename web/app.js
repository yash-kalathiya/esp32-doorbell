const $ = (id) => document.getElementById(id);
let state = null, viewing = false, busy = false, connectingAt = 0;
let updatesConnected = false;
let faceOverlay = null, faceOverlayUntil = 0, faceOverlayTimer = null, faceOverlayService = null;
let autoLive = localStorage.getItem('doorbell-auto-live') !== 'false';
$('auto-live').checked = autoLive;
const text = (id, value) => { $(id).textContent = value; };
const duration = (ms) => {
  const minutes = Math.floor(ms / 60000);
  if (minutes < 1) return `${Math.floor(ms / 1000)} sec`;
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
};
const sensorDuration = (ms) => ms === 0 ? '<1 ms' : ms < 1000 ? `${Math.round(ms)} ms` : duration(ms);
function renderSensorTiming() {
  const timing = state?.sensor_activity;
  for (const [kind, title] of [['motion', 'PIR'], ['sound', 'Sound']]) {
    const row = timing?.[kind];
    text(`${kind}-timing`, !timing?.supported ? `${title}: timing firmware update needed` : !row ? `${title}: unavailable` :
      `${title}: ${row.level ? 'HIGH' : 'LOW'}${row.level === row.active_level ? ' · active' : ' · idle'}${row.level_for_ms == null ? ' · last known' : ' for ' + sensorDuration(row.level_for_ms)} · ${row.trigger_count} alert${row.trigger_count === 1 ? '' : 's'} this boot${(row.active_level ? row.last_high_ms : row.last_low_ms) != null ? ' · last active ' + sensorDuration(row.active_level ? row.last_high_ms : row.last_low_ms) : ''}`);
  }
  text('timing-help', !timing?.supported ? 'Upload night-light-1 to measure levels and completed activity periods.' : timing.stale ? 'Camera status is stale; live duration is paused.' : state?.camera?.sensor_policy ? 'PIR triggers on HIGH. Sound pulses form one incident until quiet for 2 seconds. Automatic video is capped at 30 seconds. Sound levels are sampled every 20 ms during interrupt holdoff; shorter changes may be missed.' : 'PIR triggers on HIGH. Sound triggers on LOW; upload reliable-1 for incident grouping.');
}
function toast(message) {
  text('toast', message); $('toast').hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { $('toast').hidden = true; }, 6500);
}
async function api(path, options = {}) {
  const response = await fetch(path, {...options, signal: AbortSignal.timeout(12000)});
  if (!response.ok) {
    const result = await response.json().catch(() => ({}));
    throw Error(typeof result.detail === 'string' ? result.detail : 'Check the camera IP address and try again.');
  }
  return response.json();
}
function setViewing(on) {
  if (on === viewing) return;
  viewing = on;
  if (on) {
    connectingAt = Date.now();
    $('stream').src = '/api/stream?t=' + Date.now();
  } else {
    $('stream').removeAttribute('src');
    $('stream').hidden = true;
    $('placeholder').hidden = false;
    $('viewport').classList.remove('playing');
    paintFaceOverlay();
  }
}
$('stream').addEventListener('load', () => {
  if (!viewing) return;
  $('stream').hidden = false; $('placeholder').hidden = true;
  $('viewport').classList.add('playing'); text('video-state', 'Live'); $('video-state').classList.add('live');
  paintFaceOverlay();
});
$('stream').addEventListener('error', () => {
  $('stream').hidden = true; $('placeholder').hidden = false;
  paintFaceOverlay();
});
let nightBusy = false;
function render() {
  const lighting = state?.camera_controls;
  $('night-mode').setAttribute('aria-pressed', String(Boolean(lighting?.night_mode)));
  $('night-mode').disabled = nightBusy;
  text('lighting-status', `Auto light · ${lighting?.brightness == null ? 'Checking light levels' : lighting?.light_percent ? '50% in low light' : 'Off — enough light'} · Night mode ${lighting?.night_mode ? 'on' : 'off'}${lighting?.applied ? '' : ' · Waiting for camera'}`);
  paintFaceOverlay();
  renderSensorTiming();
  renderCsi();
  const online = Boolean(state?.online), c = state?.camera || {};
  $('connection').className = 'connection ' + (online ? 'online' : 'offline');
  text('connection-label', online ? 'Camera connected' : 'Camera offline');
  const mqtt = state?.mqtt || {};
  text('mqtt-broker-state', !mqtt.configured ? 'Not configured' : mqtt.broker_connected ? 'Connected · TLS' : 'Disconnected');
  text('mqtt-device-state', mqtt.device_online ? 'Online' : 'Waiting for device');
  text('control-transport', state?.control_transport === 'mqtt' ? 'MQTT · ACK required' : 'HTTP fallback');
  text('dashboard-updates', updatesConnected ? 'Live · SSE' : 'Polling fallback');
  text('mqtt-detail', !mqtt.configured ? 'MQTT setup is available from the project launcher.' : mqtt.error || (!mqtt.broker_connected ? 'Local MQTT broker unavailable. HTTP fallback remains active.' : mqtt.device_online ? `${mqtt.events_received} MQTT event deliveries · pending device events: ${c.mqtt_pending_events ?? '—'} · lost: ${c.mqtt_events_lost ?? '—'}` : 'Broker connected. The ESP32 must be running the MQTT firmware on the same LAN.'));
  $('start').disabled = !online || busy || Boolean(c.manual_live);
  $('stop').disabled = !online || busy || (!viewing && !c.manual_live);
  $('snapshot').disabled = !online || busy;
  const shouldView = online && (Boolean(c.manual_live) || (autoLive && !state?.csi?.enabled && !c.csi_pilot?.enabled && Boolean(c.stream_allowed)));
  if (!online || state?.csi?.enabled || c.csi_pilot?.enabled || (!c.manual_live && !autoLive)) setViewing(false);
  else if (shouldView && !render.paused) setViewing(true);
  else if (!shouldView) { setViewing(false); render.paused = false; }
  const receiving = viewing && Boolean(state?.video.receiving);
  if (viewing && !receiving && Date.now() - connectingAt > 5000) {
    $('stream').hidden = true; $('placeholder').hidden = false;
    $('viewport').classList.remove('playing');
  }
  text('video-state', receiving ? 'Live' : viewing ? 'Connecting' : online ? 'Standby' : 'Offline');
  $('video-state').classList.toggle('live', receiving);
  text('placeholder-title', !online ? 'Camera offline' : viewing ? 'Opening your live view…' : 'Camera on standby');
  text('placeholder-text', !online ? state?.error || 'Check the camera address and Wi-Fi connection.' : viewing ? state?.video.error || 'Waiting for video from your ESP32-CAM.' : 'Select Start live, or let sensor activity bring up the video.');
  $('placeholder-settings').hidden = online && !state?.video.error;
  text('video-footnote', receiving ? 'Live view · Relayed through your laptop' : 'Video stays on your local network');
  text('activity-footer', online ? mqtt.device_online ? 'MQTT events · live dashboard updates' : 'HTTP fallback · checking every 2 seconds' : 'History saved · Camera offline');
  const fresh = online ? '' : 'Last reported · ';
  text('wifi-value', typeof c.rssi === 'number' ? `${c.rssi} dBm` : '—');
  text('wifi-detail', c.rssi != null ? fresh + (c.rssi > -60 ? 'Strong signal' : c.rssi > -75 ? 'Fair signal' : 'Weak signal') : 'Waiting for camera');
  text('motion-value', c.sensor_policy?.pir_stuck_high ? 'Check sensor' : c.pir_enabled === false ? 'Disabled' : c.motion_active != null ? c.motion_active ? 'Active' : 'Clear' : '—');
  text('motion-detail', c.pir_enabled === false ? 'Disabled in firmware' : c.pir_ready != null ? fresh + (c.pir_ready ? 'PIR input · GPIO 13' : 'Sensor warming up') : 'Waiting for camera');
  text('sound-value', c.sound_enabled === false ? 'Disabled' : c.sound_input_level != null ? c.sound_input_level === (c.sound_active_low ? 0 : 1) ? 'Triggered' : 'Quiet' : '—');
  text('sound-detail', c.sound_input_level != null ? fresh + 'Digital threshold · no audio' : 'Digital threshold · no audio');
  text('uptime-value', c.uptime_ms != null ? duration(c.uptime_ms) : '—');
  text('uptime-detail', c.device_id ? fresh + c.device_id : 'Waiting for camera');
  const notices = [];
  if (state?.events_lost) notices.push('Some camera events were overwritten before the laptop received them.');
  if (online && c.sensor_policy?.pir_stuck_high) notices.push('PIR has stayed HIGH for too long. Check its wiring and power; automatic video is time-limited.');
  else if (online && c.motion_active) notices.push(c.sensor_policy ? 'PIR input is active. Automatic video is limited to 30 seconds; a new motion incident requires the input to clear.' : 'PIR input is active. Upload reliable-1 for bounded automatic video and sensor fault handling.');
  if (online && c.sensor_policy?.sound_noisy) notices.push('Sound input remains noisy or active. Pulses are grouped; automatic video is time-limited.');
  $('notice').hidden = !notices.length; text('notice', notices.join(' '));
}
let eventSignature = '';
function renderEvents(events) {
  events = events.filter(event => event.phase !== 'cleared' || !event.start_event_id);
  const signature = JSON.stringify(events);
  if (signature === eventSignature) return;
  eventSignature = signature; text('event-count', events.length);
  if (!events.length) {
    $('activity-list').innerHTML = '<div class="empty-state"><span class="empty-icon"><svg aria-hidden="true"><use href="#i-clock"/></svg></span><h3>No recent activity</h3><p>Motion and sound events will appear here when received.</p></div>';
    return;
  }
  $('activity-list').replaceChildren(...events.map(event => {
    const row = document.createElement('article'); row.className = 'event';
    const sound = event.type === 'sound';
    const icon = document.createElement('span'); icon.className = 'event-icon' + (sound ? ' sound' : '');
    icon.innerHTML = `<svg aria-hidden="true"><use href="#i-${sound ? 'sound' : 'motion'}"/></svg>`;
    const content = document.createElement('div'); content.className = 'event-content';
    const title = document.createElement('h3'); title.textContent = (sound ? 'Sound' : event.type === 'motion' ? 'Motion' : 'Sensor') + (event.phase === 'cleared' ? ' cleared' : ' detected');
    const detail = document.createElement('p'); detail.textContent = `Camera uptime ${duration(event.uptime_ms)} · #${event.id}${event.source ? ' · ' + event.source.toUpperCase() : ''}`;
    const timing = document.createElement('p'); timing.className = 'event-duration';
    timing.textContent = event.duration_ms != null ? `${event.active ? 'Active for' : 'Was active for'} ${sensorDuration(event.duration_ms)}${event.active ? ' · ongoing' : ' · cleared'}` : event.phase === 'cleared' ? 'Start was not received; duration unavailable.' : event.phase ? 'End time not received.' : 'Historical event · duration was not recorded.';
    content.append(title, detail, timing);
    const time = document.createElement('time'); time.dateTime = event.received_at;
    const received = new Date(event.received_at);
    time.textContent = received.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    time.title = 'First received by laptop: ' + received.toLocaleString();
    row.append(icon, content, time); return row;
  }));
}
async function refresh() {
  if (updatesConnected) { setTimeout(refresh, 2000); return; }
  try {
    const [next, activity] = await Promise.all([api('/api/status'), api('/api/events')]);
    state = next; render(); renderEvents(activity.events);
  } catch {
    state = {...state, online: false, error: 'Laptop server disconnected. Keep the server running, then reload this page.'};
    render(); text('connection-label', 'Server disconnected');
  } finally { setTimeout(refresh, 2000); }
}
async function control(action) {
  busy = true; render();
  try {
    state = await api('/api/live/' + action, {method:'POST'});
    render.paused = action === 'stop';
    if (action === 'stop') {
      setViewing(false);
      toast(state.camera.stream_allowed ? 'Live view paused here. Sensor activity may still keep the camera available.' : 'Live view stopped.');
    } else { render.paused = false; setViewing(true); }
  } catch (error) { toast(error.message); }
  finally { busy = false; render(); }
}
$('night-mode').addEventListener('click', async () => {
  if (nightBusy) return;
  nightBusy = true; render();
  try {
    const settings = await api('/api/settings');
    const enabled = !settings.night_mode;
    await api('/api/settings', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...settings, night_mode:enabled})});
    state = await api('/api/status');
    toast(`Night mode ${enabled ? 'on' : 'off'}. ${state.camera_controls.applied ? 'Camera settings applied.' : 'Settings saved; applying when the camera is connected.'}`);
  } catch (error) { toast(error.message); }
  finally { nightBusy = false; render(); }
});
$('start').addEventListener('click', () => control('start'));
$('stop').addEventListener('click', () => control('stop'));
$('auto-live').addEventListener('change', (event) => {
  autoLive = event.target.checked; localStorage.setItem('doorbell-auto-live', autoLive);
  render.paused = false; render();
});
$('snapshot').addEventListener('click', async () => {
  $('snapshot').disabled = true;
  try {
    const response = await fetch('/api/snapshot', {signal: AbortSignal.timeout(10000)});
    if (!response.ok) throw Error('Snapshot failed. Check the camera connection.');
    const url = URL.createObjectURL(await response.blob());
    const a = document.createElement('a'); a.href = url; a.download = 'doorbell-snapshot.jpg'; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000); toast('Snapshot saved.');
  } catch (error) { toast(error.message); }
  finally { render(); }
});
function openSettings() {
  text('settings-error', ''); $('camera-url').value = state?.camera_url || '';
  $('settings-dialog').showModal();
}
$('settings-open').addEventListener('click', openSettings);
$('placeholder-settings').addEventListener('click', openSettings);
document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
$('settings-form').addEventListener('submit', async event => {
  event.preventDefault(); const submit = event.submitter; submit.disabled = true;
  try {
    await api('/api/settings', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...await api('/api/settings'), camera_url:$('camera-url').value, face_monitoring:$('face-monitoring').checked})});
    setViewing(false); render.paused = false; eventSignature = '';
    $('activity-list').innerHTML = '<div class="empty-state"><span class="empty-icon"><svg aria-hidden="true"><use href="#i-clock"/></svg></span><h3>No recent activity</h3><p>Motion and sound events will appear here when received.</p></div>';
    $('settings-dialog').close(); toast('Camera address saved. Checking the connection…');
    state = await api('/api/status'); render();
  } catch(error) { text('settings-error', error.message); }
  finally { submit.disabled = false; }
});
let phoneAddress = location.origin;
$('phone-open').addEventListener('click', async () => {
  try { phoneAddress = (await api('/api/network')).phone_url || location.origin; }
  catch { phoneAddress = location.origin; }
  text('phone-address', phoneAddress); $('phone-dialog').showModal();
});
$('copy-address').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(phoneAddress); toast('Address copied.'); }
  catch { toast('Select and copy the address above.'); }
});
setInterval(() => text('video-clock', new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})), 1000);
refresh();

let faceRequest = 0, faceSettingsBusy = false, peopleSignature = '';
const faceCards = new Map();
function faceTime(value) {
  return new Date(value).toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
}
function makeUnknownCard(face) {
  const card = document.createElement('article'); card.className = 'unknown-card'; card.dataset.faceId = face.id;
  const img = document.createElement('img'); img.alt = 'Captured unrecognised face'; img.loading = 'lazy';
  const body = document.createElement('div'); body.className = 'unknown-body';
  const title = document.createElement('h4'); title.textContent = 'Unknown face';
  const seen = document.createElement('p'); seen.className = 'face-seen';
  const form = document.createElement('form'); form.className = 'enroll-form';
  const label = document.createElement('label'); label.htmlFor = 'name-' + face.id; label.textContent = 'Who is this?';
  const input = document.createElement('input'); input.id = label.htmlFor; input.name = 'name'; input.required = true;
  input.maxLength = 80; input.placeholder = 'Enter a name'; input.autocomplete = 'off'; input.setAttribute('list', 'known-names');
  const error = document.createElement('p'); error.className = 'enroll-error'; error.setAttribute('role', 'alert'); error.hidden = true;
  const button = document.createElement('button'); button.type = 'submit'; button.className = 'button primary'; button.textContent = 'Save person';
  const burstOption = document.createElement('label'); burstOption.className = 'burst-option';
  const burstCheck = document.createElement('input'); burstCheck.type = 'checkbox'; burstCheck.checked = true;
  burstOption.append(burstCheck, document.createTextNode('Add an 8-photo ESP32 camera burst'));
  form.append(label, input, burstOption, button, error);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (burstCheck.checked) { openBurst(input.value, face.id); return; }
    button.disabled = true; error.hidden = true;
    try {
      const result = await api(`/api/faces/${encodeURIComponent(face.id)}/enroll`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:input.value})});
      toast(`${result.name} saved. New camera faces will be compared with this profile.`);
      await refreshFaces();
    } catch (failure) { error.textContent = failure.message; error.hidden = false; }
    finally { button.disabled = false; }
  });
  body.append(title, seen, form); card.append(img, body); return card;
}
function renderFaceData(data) {
  faceOverlayService = data;
  faceOverlay = data.live_detections || null;
  faceOverlayUntil = performance.now() + Math.max(0, Math.min(5000, Number(faceOverlay?.valid_for_ms) || 0));
  clearTimeout(faceOverlayTimer);
  faceOverlayTimer = setTimeout(paintFaceOverlay, Math.max(0, faceOverlayUntil - performance.now()) + 1);
  paintFaceOverlay();
  if (!faceSettingsBusy) $('face-monitoring').checked = data.enabled;
  const status = $('face-status');
  status.classList.toggle('warning', Boolean(data.error));
  let message;
  if (data.model_state === 'error') message = data.error || 'Face models are unavailable.';
  else if (data.model_state === 'loading') message = 'Loading the local face models…';
  else if (!data.enabled) message = 'Face monitoring is paused. Saved profiles and captures remain available.';
  else if (!data.camera_online) message = 'Face recognition is ready. Waiting for the camera to connect.';
  else if (data.error) message = data.error;
  else if (!data.last_checked) message = 'Camera connected. Waiting for the first face check…';
  else message = `Monitoring · Last check ${faceTime(data.last_checked)} · ${data.faces_in_last_check} usable face${data.faces_in_last_check === 1 ? '' : 's'}${data.low_quality_faces ? ' · Move closer or improve lighting for small/blurry faces.' : ''}`;
  status.textContent = message;
  text('unknown-count', data.unknowns.length); text('people-count', data.people.length);
  const container = $('unknown-faces'), present = new Set(data.unknowns.map(face => face.id));
  for (const [id, card] of faceCards) {
    if (!present.has(id)) { card.remove(); faceCards.delete(id); }
  }
  if (data.unknowns.length) container.querySelector('.faces-empty')?.remove();
  for (const face of data.unknowns) {
    let card = faceCards.get(face.id);
    if (!card) { card = makeUnknownCard(face); faceCards.set(face.id, card); container.append(card); }
    const image = card.querySelector('img');
    if (image.getAttribute('src') !== face.image_url) image.src = face.image_url;
    card.querySelector('.face-seen').textContent = 'Last seen ' + faceTime(face.last_seen);
  }
  if (!data.unknowns.length && !container.querySelector('.faces-empty')) {
    const empty = document.createElement('p'); empty.className = 'faces-empty';
    empty.textContent = 'No unrecognised faces to review. New captures appear here automatically.'; container.append(empty);
  }
  const signature = JSON.stringify(data.people);
  if (signature !== peopleSignature) {
    peopleSignature = signature;
    $('known-names').replaceChildren(...data.people.map(person => {const option = document.createElement('option'); option.value = person.name; return option;}));
    const profiles = data.people.map(person => {
      const card = document.createElement('article'); card.className = 'person-card';
      const image = document.createElement('img'); image.src = person.image_url; image.alt = `Saved photo for ${person.name}`; image.loading = 'lazy';
      const info = document.createElement('div'); info.className = 'person-description';
      const name = document.createElement('h4'); name.textContent = person.name;
      const samples = document.createElement('p'); samples.textContent = `${person.samples} saved sample${person.samples === 1 ? '' : 's'}`;
      info.append(name, samples);
      const remove = document.createElement('button'); remove.className = 'remove-person'; remove.textContent = 'Remove'; remove.setAttribute('aria-label', `Remove ${person.name}`);
      remove.addEventListener('click', async () => {
        if (!confirm(`Remove ${person.name}'s profile, saved samples, and associated face history?`)) return;
        remove.disabled = true;
        try { await api(`/api/people/${encodeURIComponent(person.id)}`, {method:'DELETE'}); toast('Profile removed. Future sightings can appear as unknown.'); await refreshFaces(); }
        catch (error) { toast(error.message); remove.disabled = false; }
      });
      const add = document.createElement('button'); add.className = 'add-person-photos'; add.textContent = 'Add photos'; add.setAttribute('aria-label', `Add photos for ${person.name}`);
      add.addEventListener('click', () => openUpload(person.name));
      const actions = document.createElement('div'); actions.className = 'person-actions'; actions.append(add, remove);
      card.append(image, info, actions); return card;
    });
    if (!profiles.length) {
      const empty = document.createElement('p'); empty.className = 'faces-empty'; empty.textContent = 'No profiles yet. Save a name from an unknown face above.'; profiles.push(empty);
    }
    $('known-people').replaceChildren(...profiles);
  }
  $('recent-faces').hidden = !data.recent.length;
  $('recognised-list').replaceChildren(...data.recent.map(face => {
    const item = document.createElement('div'); item.className = 'recognised-item';
    const image = document.createElement('img'); image.src = face.image_url; image.alt = ''; image.loading = 'lazy';
    const name = document.createElement('strong'); name.textContent = face.name;
    const seen = document.createElement('span'); seen.textContent = faceTime(face.last_seen);
    item.append(image, name, seen); return item;
  }));
}
function paintFaceOverlay() {
  const layer = $('face-overlay'), caption = $('face-overlay-status'), image = $('stream');
  if (!layer) return;
  layer.replaceChildren(); layer.setAttribute('hidden', '');
  const showing = viewing && !image.hidden && Boolean(state?.online) && Boolean(state?.video.receiving);
  caption.hidden = !showing;
  if (!showing) return;
  const service = faceOverlayService;
  caption.textContent = !service ? 'Waiting for face check' : !service.enabled ? 'Face monitoring paused' : service.error ? 'Face checks unavailable' : 'Waiting for face check';
  const data = faceOverlay;
  if (!service?.enabled || service.error || !data || performance.now() >= faceOverlayUntil) return;
  const width = Number(data.image_width), height = Number(data.image_height);
  if (!(width > 0 && height > 0) || !Number.isFinite(width + height)) return;
  if (image.naturalWidth && Math.abs(image.naturalWidth / image.naturalHeight - width / height) > .01) return;
  layer.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const make = (tag, attributes) => {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attributes).forEach(([name, value]) => node.setAttribute(name, value));
    return node;
  };
  for (const face of (data.faces || []).slice(0, 12)) {
    if (!Array.isArray(face.box) || face.box.length !== 4 || !face.box.every(Number.isFinite)) continue;
    const [rawX, rawY, rawW, rawH] = face.box;
    const x = Math.max(0, rawX), y = Math.max(0, rawY);
    const w = Math.min(width, rawX + rawW) - x, h = Math.min(height, rawY + rawH) - y;
    if (!(w > 0 && h > 0)) continue;
    const group = make('g', {'class': face.matched ? 'matched-face' : 'unknown-face'});
    group.append(make('rect', {x, y, width: w, height: h, 'class': 'face-box', 'stroke-width': Math.max(1.5, width / 400)}));
    const font = Math.max(8, Math.min(14, width / 40)), line = font * 1.6;
    const maxChars = Math.max(1, Math.floor((width - x - font) / (font * .62)));
    const full = String(face.label || 'Unknown');
    const label = full.length > maxChars ? full.slice(0, Math.max(0, maxChars - 1)) + '…' : full;
    const labelY = Math.max(0, y - line);
    group.append(make('rect', {x, y: labelY, width: Math.min(width - x, label.length * font * .62 + font), height: line, 'class': 'face-label-bg'}));
    const text = make('text', {x: x + font / 2, y: labelY + line / 2, 'font-size': font, 'dominant-baseline': 'central', 'class': 'face-label'});
    text.textContent = label; group.append(text); layer.append(group);
  }
  layer.removeAttribute('hidden');
  caption.textContent = `Face checks ~1s · ${layer.childElementCount ? 'Latest detection' : 'No face found'}`;
}
async function refreshFaces() {
  const sequence = ++faceRequest;
  try {
    const data = await api('/api/faces');
    if (sequence === faceRequest) renderFaceData(data);
  } catch {
    if (sequence === faceRequest) { text('face-status', 'Face service unavailable. Check that the laptop server is running.'); $('face-status').classList.add('warning'); }
  }
}
$('face-monitoring').addEventListener('change', async event => {
  const input = event.target; input.disabled = true; faceSettingsBusy = true;
  try {
    const settings = await api('/api/settings');
    await api('/api/settings', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...settings, face_monitoring:input.checked})});
    toast(input.checked ? 'Face monitoring enabled.' : 'Face monitoring paused.');
  } catch(error) { input.checked = !input.checked; toast(error.message); }
  finally { input.disabled = false; faceSettingsBusy = false; await refreshFaces(); }
});
async function pollFaces() { if (!updatesConnected) await refreshFaces(); setTimeout(pollFaces, 3000); }
pollFaces();

let csiBusy = false;
function renderCsi() {
  const pilot = state?.csi || {}, camera = state?.camera || {};
  const titles = {stopped:'Stopped', no_data:'No data', low_rate:'Low packet rate', calibrating:'Calibrating', uncalibrated:'Uncalibrated', waiting:'Collecting', elevated:'Elevated signal', quiet:'Quiet signal'};
  text('csi-state', titles[pilot.state] || 'No data');
  const supported = Boolean(camera.csi_pilot?.supported);
  $('csi-start').disabled = csiBusy || !state?.online || !supported || Boolean(pilot.enabled);
  $('csi-stop').disabled = csiBusy || (!pilot.enabled && !camera.csi_pilot?.enabled);
  $('csi-calibrate').disabled = csiBusy || !pilot.enabled || pilot.rate_hz < 5 || pilot.state === 'calibrating';
  $('csi-empty').disabled = $('csi-walking').disabled = csiBusy || !pilot.enabled || pilot.rate_hz < 5 || Boolean(pilot.trial);
  const info = !supported ? 'Upload reliable-1 to enable the CSI pilot.' : !state?.online ? 'Camera offline. Reconnect it before starting.' : pilot.error || pilot.calibration_error || (pilot.state === 'calibrating' ? `Keep the room empty · ${pilot.calibration_remaining_s}s remaining.` : pilot.state === 'uncalibrated' ? 'Live packets are arriving. Leave the room, then calibrate for 60 seconds.' : pilot.state === 'low_rate' ? 'Need at least 5 packets/s for analysis. Check the radio link and router responses.' : pilot.state === 'no_data' ? 'No fresh CSI packets. Check the pilot, UDP port 5005 and camera connection.' : pilot.enabled ? `Live CSI signal changes · ${camera.csi_pilot?.target_hz || 10} Hz capture target · video paused · automatic stop after 10 minutes.` : pilot.reason || 'Ready to start.');
  text('csi-help', info + (!pilot.enabled && (state?.video?.viewers || camera.manual_live) ? ' Stop live video before starting CSI.' : ''));
  text('csi-rate', pilot.enabled ? `${pilot.rate_hz || 0} Hz` : '—');
  text('csi-rssi', pilot.rssi != null ? `${pilot.rssi} dBm` : '—');
  text('csi-gaps', pilot.enabled ? String(pilot.sequence_gaps || 0) : '—');
  text('csi-threshold', pilot.threshold != null ? pilot.threshold.toFixed(4) : '—');
  const memory = camera.diagnostics?.memory || {}, diagnostics = camera.diagnostics?.camera || {};
  text('csi-heap', memory.internal_min_free_bytes != null ? `${memory.internal_min_free_bytes} B` : '—');
  text('csi-stream-errors', diagnostics.stream_errors != null ? String(diagnostics.stream_errors) : '—');
  const history = pilot.history || [], scores = history.filter(p => p.score != null).map(p => p.score);
  const ceiling = Math.max(.03, (pilot.threshold || 0) * 1.4, ...scores.map(s => s * 1.15));
  let path = '', pen = false;
  history.forEach((point,index) => {
    if (point.score == null) { pen = false; return; }
    const x = 10 + (59 - history.length + 1 + index) / 59 * 580, y = 110 - point.score / ceiling * 100;
    path += `${pen ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)} `; pen = true;
  });
  $('csi-line').setAttribute('d', path);
  const y = 110 - (pilot.threshold || 0) / ceiling * 100;
  $('csi-threshold-line').setAttribute('d', pilot.threshold != null ? `M10,${y} L590,${y}` : '');
  $('csi-plot-empty').hidden = Boolean(scores.length);
  const comparison = pilot.comparison;
  $('csi-comparison').hidden = !comparison;
  if (comparison) text('csi-comparison', `Recent trials with video ${comparison.video_running ? 'running' : 'stopped'} · Empty ${comparison.empty_mean.toFixed(4)} · Walking ${comparison.walking_mean.toFixed(4)}${comparison.ratio != null ? ' · ' + comparison.ratio.toFixed(1) + '× change' : ''}. One comparison does not establish detection accuracy or range.`);
  text('csi-trial-progress', pilot.trial ? `Recording ${pilot.trial.label} · ${pilot.trial.remaining_s}s remaining` : '');
  $('csi-recordings').replaceChildren(...(pilot.runs || []).map(run => {
    const row = document.createElement('li'), link = document.createElement('a');
    link.href = `/api/csi/recordings/${encodeURIComponent(run.id)}`;
    link.textContent = `${run.label === 'empty' ? 'Empty room' : 'Walking'} · ${run.valid_windows} valid windows${run.completed ? '' : ' · interrupted'} · CSV`;
    row.append(link); return row;
  }));
}
for (const [id,action] of [['csi-start','start'],['csi-stop','stop'],['csi-calibrate','calibrate'],['csi-empty','record-empty'],['csi-walking','record-walking']]) {
  $(id).addEventListener('click', async () => {
    csiBusy = true; renderCsi();
    try {
      await api('/api/csi/' + action, {method:'POST'});
      state = await api('/api/status'); render();
    } catch(error) { toast(error.message); }
    finally { csiBusy = false; renderCsi(); }
  });
}

let burstFiles = [], burstUrls = [], burstAnchor = '', burstEpoch = 0, burstBusy = false, burstController = null;
function clearBurst() {
  burstEpoch += 1; burstController?.abort(); burstController = null;
  burstUrls.forEach(url => URL.revokeObjectURL(url)); burstUrls = []; burstFiles = [];
  $('burst-previews').replaceChildren(); burstBusy = false;
}
function openBurst(name = '', anchor = '') {
  clearBurst(); burstAnchor = anchor; $('burst-form').reset(); $('burst-name').value = name;
  $('burst-start').disabled = false; $('burst-save').disabled = true; $('burst-name').disabled = false;
  $('burst-error').hidden = true; $('burst-results').hidden = true; $('burst-results').replaceChildren();
  $('burst-review').hidden = true; text('burst-progress', 'Make sure the person is ready at the ESP32 camera.');
  $('burst-dialog').showModal();
}
$('burst-open').addEventListener('click', () => openBurst());
$('burst-dialog').addEventListener('close', clearBurst);
$('burst-start').addEventListener('click', async () => {
  if (burstBusy) return;
  clearBurst(); const epoch = burstEpoch; burstBusy = true;
  $('burst-start').disabled = true; $('burst-save').disabled = true; $('burst-error').hidden = true;
  $('burst-results').hidden = true; $('burst-review').hidden = true;
  const prompts = ['Face forward', 'Keep your face clear', 'Turn slightly left', 'Hold that angle', 'Turn slightly right', 'Hold that angle', 'Move a little closer', 'Face forward again'];
  try {
    for (let i = 0; i < 8; i++) {
      if (epoch !== burstEpoch) return;
      text('burst-progress', `Photo ${i + 1} of 8 · ${prompts[i]}`);
      await new Promise(resolve => setTimeout(resolve, 1000));
      if (epoch !== burstEpoch) return;
      burstController = new AbortController();
      const timer = setTimeout(() => burstController?.abort(), 10000);
      let response;
      try { response = await fetch('/api/enrollment/frame', {cache:'no-store', signal:burstController.signal}); }
      finally { clearTimeout(timer); }
      if (!response.ok) { const body = await response.json().catch(() => ({})); throw Error(body.detail || 'The ESP32 could not supply a photo.'); }
      const blob = await response.blob();
      if (epoch !== burstEpoch) return;
      if (blob.size > 4 * 1024 * 1024 || !blob.type.startsWith('image/jpeg')) throw Error('Camera did not return a usable JPEG.');
      const file = new File([blob], `burst-${i+1}.jpg`, {type:'image/jpeg'}); burstFiles.push(file);
      const url = URL.createObjectURL(blob); burstUrls.push(url);
      const label = document.createElement('label'); label.className = 'burst-preview';
      const image = document.createElement('img'); image.src = url; image.alt = `Burst photo ${i+1}`;
      const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = true;
      checkbox.setAttribute('aria-label', `Keep burst photo ${i+1}`);
      checkbox.addEventListener('change', () => { $('burst-save').disabled = !burstFiles.length || !document.querySelector('#burst-previews input:checked'); });
      label.append(image, checkbox); $('burst-previews').append(label);
    }
    if (epoch === burstEpoch) text('burst-progress', '8 photos captured. Review the photos, then save.');
  } catch (error) {
    if (epoch === burstEpoch) { text('burst-error', error.name === 'AbortError' ? 'Camera capture timed out. Check its connection and retry.' : error.message); $('burst-error').hidden = false; }
  } finally {
    if (epoch === burstEpoch) {
      burstBusy = false; $('burst-start').disabled = false;
      $('burst-save').disabled = !burstFiles.length || !document.querySelector('#burst-previews input:checked'); $('burst-review').hidden = !burstFiles.length;
    }
  }
});
$('burst-form').addEventListener('submit', async event => {
  event.preventDefault(); if (burstBusy || !burstFiles.length) return;
  const selected = [...document.querySelectorAll('#burst-previews input')].flatMap((input,index) => input.checked ? [burstFiles[index]] : []);
  if (!selected.length) return;
  const form = new FormData(); form.append('name', $('burst-name').value); form.append('burst','true');
  if (burstAnchor) form.append('capture_id',burstAnchor);
  selected.forEach(file => form.append('files',file));
  burstBusy = true; const epoch = burstEpoch;
  $('burst-name').disabled = true; $('burst-start').disabled = true; $('burst-save').disabled = true;
  $('burst-error').hidden = true; text('burst-progress','Checking photos and saving usable samples…');
  try {
    const response = await fetch('/api/people/upload', {method:'POST',body:form,signal:AbortSignal.timeout(120000)});
    const data = await response.json();
    if (epoch !== burstEpoch) { if (response.ok) { toast('Camera burst saved.'); await refreshFaces(); } return; }
    $('burst-results').replaceChildren(...(data.results || []).map(result => {
      const row = document.createElement('li'); row.className = result.status === 'added' ? 'upload-added' : result.status === 'skipped' ? 'upload-skipped' : 'upload-rejected';
      const label = document.createElement('strong'); label.textContent = result.filename;
      const reason = document.createElement('span'); reason.textContent = result.reason; row.append(label,reason); return row;
    }));
    $('burst-results').hidden = !(data.results || []).length;
    if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : 'Burst enrollment failed.');
    text('burst-progress', `${data.person.name}: ${data.samples_added} new sample${data.samples_added === 1 ? '' : 's'} saved${burstAnchor ? ', including the reviewed face' : ''}. Near-duplicates are skipped.`);
    toast(`${data.person.name} saved with ${data.samples_added} new sample${data.samples_added === 1 ? '' : 's'}.`);
    await refreshFaces(); burstFiles = []; $('burst-save').disabled = true;
  } catch(error) {
    if (epoch === burstEpoch) { text('burst-error', error.name === 'TimeoutError' ? 'Save timed out. Refresh People before retrying.' : error.message); $('burst-error').hidden = false; $('burst-save').disabled = false; }
  } finally {
    if (epoch === burstEpoch) { burstBusy = false; $('burst-name').disabled = false; $('burst-start').disabled = false; }
  }
});

let uploadBusy = false, previewUrls = [], uploadPending = [];

function* photoUploadBatches(files) {
  let batch = [], bytes = 0;
  for (const file of files) {
    if (batch.length && (batch.length === 10 || bytes + file.size > 40 * 1024 * 1024)) {
      yield batch; batch = []; bytes = 0;
    }
    batch.push(file); bytes += file.size;
  }
  if (batch.length) yield batch;
}
function clearUploadPreviews() {
  previewUrls.forEach(url => URL.revokeObjectURL(url)); previewUrls = [];
  $('upload-previews').replaceChildren();
}
function openUpload(name = '') {
  if (uploadBusy) return;
  $('upload-form').reset(); uploadPending = []; $('upload-name').value = name;
  $('upload-message').hidden = true; $('upload-results').hidden = true;
  $('upload-results').replaceChildren(); clearUploadPreviews();
  $('upload-submit').disabled = true; $('upload-dialog').showModal();
}
$('upload-open').addEventListener('click', () => openUpload());
$('upload-dialog').addEventListener('close', clearUploadPreviews);
$('upload-files').addEventListener('change', event => {
  clearUploadPreviews(); $('upload-results').hidden = true; $('upload-message').hidden = true;
  const files = Array.from(event.target.files);
  uploadPending = [];
  const error = files.some(file => file.size > 8 * 1024 * 1024) ? 'Each photo must be smaller than 8 MB.' : '';
  if (error) {
    text('upload-message', error); $('upload-message').classList.remove('success'); $('upload-message').hidden = false;
    $('upload-submit').disabled = true; return;
  }
  uploadPending = files;
  // Bound preview decoding without limiting how many photos can be uploaded.
  for (const file of files.slice(0, 24)) {
    const figure = document.createElement('figure');
    const image = document.createElement('img'); image.alt = `Selected photo: ${file.name}`;
    image.addEventListener('error', () => {
      image.hidden = true;
      const fallback = document.createElement('span'); fallback.className = 'photo-preview-unavailable';
      fallback.textContent = 'Preview unavailable · you can still upload'; figure.prepend(fallback);
    }, {once:true});
    const url = URL.createObjectURL(file); previewUrls.push(url); image.src = url;
    const caption = document.createElement('figcaption'); caption.textContent = file.name;
    figure.append(image, caption); $('upload-previews').append(figure);
  }
  if (files.length > 24) {
    const note = document.createElement('p'); note.textContent = `${files.length} photos selected. Previewing the first 24.`;
    $('upload-previews').append(note);
  }
  $('upload-submit').disabled = !files.length;
});
$('upload-form').addEventListener('submit', async event => {
  event.preventDefault(); if (uploadBusy) return;
  const files = [...uploadPending]; if (!files.length) return;
  const name = $('upload-name').value;
  uploadBusy = true; $('upload-submit').disabled = true; $('upload-open').disabled = true;
  $('upload-name').disabled = true; $('upload-files').disabled = true;
  $('upload-message').classList.remove('success'); $('upload-message').hidden = false;
  $('upload-results').replaceChildren(); $('upload-results').hidden = true;
  let added = 0, processed = 0;
  const reasons = new Set();
  try {
    for (const batch of photoUploadBatches(files)) {
      text('upload-message', `Checking photos ${processed + 1}–${processed + batch.length} of ${files.length} · ${added} added…`);
      const form = new FormData(); form.append('name', name);
      batch.forEach(file => form.append('files', file));
      const response = await fetch('/api/people/upload', {method:'POST', body:form, signal:AbortSignal.timeout(120000)});
      const result = await response.json();
      // A per-photo rejection is a completed batch. Other failures leave the
      // unconfirmed batch pending and preserve earlier confirmed additions.
      if (!response.ok && !(response.status === 422 && Array.isArray(result.results))) {
        throw Error(typeof result.detail === 'string' ? result.detail : 'Upload failed. Check the name and photos, then try again.');
      }
      for (const item of result.results || []) {
        const row = document.createElement('li'); row.className = item.status === 'added' ? 'upload-added' : 'upload-rejected';
        const filename = document.createElement('strong'); filename.textContent = item.filename;
        const reason = document.createElement('span'); reason.textContent = item.reason;
        row.append(filename, reason); $('upload-results').append(row);
        if (item.status !== 'added') reasons.add(item.reason);
      }
      added += result.samples_added || 0; processed += batch.length;
      uploadPending = files.slice(processed);
      $('upload-results').hidden = !$('upload-results').children.length;
      await refreshFaces();
    }
    const rejected = reasons.size ? ` Some photos were rejected; check the results below.` : '';
    text('upload-message', added ? `${added} photo${added === 1 ? '' : 's'} added for ${name}.${rejected}` : `No photos were added. ${[...reasons].join(' ')}`);
    $('upload-message').classList.toggle('success', added > 0);
    $('upload-files').value = ''; clearUploadPreviews();
    if (added) toast(`${name} updated with ${added} photo${added === 1 ? '' : 's'}.`);
  } catch(error) {
    const detail = error.name === 'TimeoutError' ? 'Upload timed out. Check the saved profile before retrying the unconfirmed batch.' : error.message;
    text('upload-message', `${added} photos confirmed added; ${uploadPending.length} remain unconfirmed. ${detail}`);
  } finally {
    uploadBusy = false; $('upload-open').disabled = false; $('upload-name').disabled = false; $('upload-files').disabled = false;
    $('upload-submit').disabled = !uploadPending.length;
  }
});

if (window.EventSource) {
  const updates = new EventSource('/api/updates');
  updates.addEventListener('open', () => { updatesConnected = true; text('dashboard-updates', 'Live · SSE'); });
  updates.addEventListener('status', event => { state = JSON.parse(event.data); render(); });
  updates.addEventListener('activity', event => renderEvents(JSON.parse(event.data).events));
  updates.addEventListener('faces', event => renderFaceData(JSON.parse(event.data)));
  updates.addEventListener('error', () => { updatesConnected = false; text('dashboard-updates', 'Polling fallback'); });
  window.addEventListener('pagehide', () => updates.close());
}

// Keep navigation anchored to the section being viewed.
if ('IntersectionObserver' in window) {
  const links = [...document.querySelectorAll('nav a')];
  const observer = new IntersectionObserver(entries => {
    const entry = entries.filter(item => item.isIntersecting).sort((a,b) => b.intersectionRatio-a.intersectionRatio)[0];
    if (!entry) return;
    links.forEach(link => {
      const active = link.hash === '#' + entry.target.id;
      link.classList.toggle('nav-active', active);
      if (active) link.setAttribute('aria-current', 'location'); else link.removeAttribute('aria-current');
    });
  }, {rootMargin:'-120px 0px -40% 0px', threshold:[0,.25,.5]});
  links.forEach(link => observer.observe(document.querySelector(link.hash)));
}
