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
  text('timing-help', !timing?.supported ? 'Upload night-light-1 to measure levels and completed activity periods.' : timing.stale ? 'Camera status is stale; live duration is paused.' : 'PIR triggers on HIGH. Sound triggers on LOW; short pulses are grouped by its alert cooldown.');
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
  const shouldView = online && (Boolean(c.manual_live) || (autoLive && Boolean(c.stream_allowed)));
  if (!online || (!c.manual_live && !autoLive)) setViewing(false);
  else if (shouldView && !render.paused) setViewing(true);
  else if (!shouldView) { setViewing(false); render.paused = false; }
  const receiving = viewing && Boolean(state?.video.receiving);
  if (viewing && !receiving && Date.now() - connectingAt > 5000) {
    $('stream').hidden = true; $('placeholder').hidden = false;
    $('viewport').classList.remove('playing');
  }
  text('video-state', receiving ? 'Live' : viewing ? 'Connecting' : online ? 'Standby' : 'Offline');
  $('video-state').classList.toggle('live', receiving);
  text('placeholder-title', !online ? 'Let’s connect your camera.' : viewing ? 'Opening your live view…' : 'A clear view, whenever you need it.');
  text('placeholder-text', !online ? state?.error || 'Check the camera address and Wi-Fi connection.' : viewing ? state?.video.error || 'Waiting for video from your ESP32-CAM.' : 'Select Start live, or let sensor activity bring up the video.');
  $('placeholder-settings').hidden = online && !state?.video.error;
  text('video-footnote', receiving ? 'Live view · Relayed through your laptop' : 'Video stays on your local network');
  text('activity-footer', online ? mqtt.device_online ? 'MQTT events · live dashboard updates' : 'HTTP fallback · checking every 2 seconds' : 'History saved · Camera offline');
  const fresh = online ? '' : 'Last reported · ';
  text('wifi-value', typeof c.rssi === 'number' ? `${c.rssi} dBm` : '—');
  text('wifi-detail', c.rssi != null ? fresh + (c.rssi > -60 ? 'Strong signal' : c.rssi > -75 ? 'Fair signal' : 'Weak signal') : 'Waiting for camera');
  text('motion-value', c.pir_enabled === false ? 'Disabled' : c.motion_active != null ? c.motion_active ? 'Active' : 'Clear' : '—');
  text('motion-detail', c.pir_enabled === false ? 'Disabled in firmware' : c.pir_ready != null ? fresh + (c.pir_ready ? 'PIR input · GPIO 13' : 'Sensor warming up') : 'Waiting for camera');
  text('sound-value', c.sound_enabled === false ? 'Disabled' : c.sound_input_level != null ? c.sound_input_level === (c.sound_active_low ? 0 : 1) ? 'Triggered' : 'Quiet' : '—');
  text('sound-detail', c.sound_input_level != null ? fresh + 'Digital threshold · no audio' : 'Digital threshold · no audio');
  text('uptime-value', c.uptime_ms != null ? duration(c.uptime_ms) : '—');
  text('uptime-detail', c.device_id ? fresh + c.device_id : 'Waiting for camera');
  const notices = [];
  if (state?.events_lost) notices.push('Some camera events were overwritten before the laptop received them.');
  if (online && c.motion_active) notices.push('PIR input is active. A continuously HIGH signal keeps the activity window open; new motion entries appear after it clears and triggers again.');
  $('notice').hidden = !notices.length; text('notice', notices.join(' '));
}
let eventSignature = '';
function renderEvents(events) {
  events = events.filter(event => event.phase !== 'cleared' || !event.start_event_id);
  const signature = JSON.stringify(events);
  if (signature === eventSignature) return;
  eventSignature = signature; text('event-count', events.length);
  if (!events.length) {
    $('activity-list').innerHTML = '<div class="empty-state"><span class="empty-icon"><svg aria-hidden="true"><use href="#i-clock"/></svg></span><h3>Quiet for now</h3><p>Motion and sound events will appear here when received.</p></div>';
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
    $('activity-list').innerHTML = '<div class="empty-state"><span class="empty-icon"><svg aria-hidden="true"><use href="#i-clock"/></svg></span><h3>Quiet for now</h3><p>Motion and sound events will appear here when received.</p></div>';
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
  form.append(label, input, button, error);
  form.addEventListener('submit', async event => {
    event.preventDefault(); button.disabled = true; error.hidden = true;
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

let uploadBusy = false, previewUrls = [];
function clearUploadPreviews() {
  previewUrls.forEach(url => URL.revokeObjectURL(url)); previewUrls = [];
  $('upload-previews').replaceChildren();
}
function openUpload(name = '') {
  if (uploadBusy) return;
  $('upload-form').reset(); $('upload-name').value = name;
  $('upload-message').hidden = true; $('upload-results').hidden = true;
  $('upload-results').replaceChildren(); clearUploadPreviews();
  $('upload-submit').disabled = true; $('upload-dialog').showModal();
}
$('upload-open').addEventListener('click', () => openUpload());
$('upload-dialog').addEventListener('close', clearUploadPreviews);
$('upload-files').addEventListener('change', event => {
  clearUploadPreviews(); $('upload-results').hidden = true; $('upload-message').hidden = true;
  const files = Array.from(event.target.files);
  const total = files.reduce((bytes, file) => bytes + file.size, 0);
  let error = '';
  if (files.length > 10) error = 'Choose up to 10 photos at a time.';
  else if (files.some(file => file.size > 8 * 1024 * 1024)) error = 'Each photo must be smaller than 8 MB.';
  else if (total > 40 * 1024 * 1024) error = 'Choose photos totalling no more than 40 MB.';
  if (error) {
    text('upload-message', error); $('upload-message').classList.remove('success'); $('upload-message').hidden = false;
    $('upload-submit').disabled = true; return;
  }
  for (const file of files) {
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
  $('upload-submit').disabled = !files.length;
});
$('upload-form').addEventListener('submit', async event => {
  event.preventDefault(); if (uploadBusy) return;
  const files = Array.from($('upload-files').files); if (!files.length) return;
  const form = new FormData(); form.append('name', $('upload-name').value);
  files.forEach(file => form.append('files', file));
  uploadBusy = true; $('upload-submit').disabled = true; $('upload-open').disabled = true;
  $('upload-name').disabled = true; $('upload-files').disabled = true;
  $('upload-message').classList.remove('success'); $('upload-message').hidden = false;
  text('upload-message', `Processing ${files.length} photo${files.length === 1 ? '' : 's'}…`);
  $('upload-results').hidden = true;
  try {
    const response = await fetch('/api/people/upload', {method:'POST', body:form, signal:AbortSignal.timeout(120000)});
    const result = await response.json();
    $('upload-results').replaceChildren(...(result.results || []).map(item => {
      const row = document.createElement('li'); row.className = item.status === 'added' ? 'upload-added' : 'upload-rejected';
      const filename = document.createElement('strong'); filename.textContent = item.filename;
      const reason = document.createElement('span'); reason.textContent = item.reason;
      row.append(filename, reason); return row;
    }));
    $('upload-results').hidden = !(result.results || []).length;
    if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : 'Upload failed. Check the name and photos, then try again.');
    text('upload-message', `${result.samples_added} photo${result.samples_added === 1 ? '' : 's'} added for ${result.person.name}.`);
    $('upload-message').classList.add('success'); $('upload-files').value = ''; clearUploadPreviews();
    await refreshFaces(); toast(`${result.person.name} updated with ${result.samples_added} photo${result.samples_added === 1 ? '' : 's'}.`);
  } catch(error) {
    text('upload-message', error.name === 'TimeoutError' ? 'Upload timed out. Refresh the saved profile before retrying.' : error.message);
  } finally {
    uploadBusy = false; $('upload-open').disabled = false; $('upload-name').disabled = false; $('upload-files').disabled = false;
    $('upload-submit').disabled = !$('upload-files').files.length;
  }
});

let testPreviewUrl = null, testBusy = false, selectedTestPhoto = null;
let deviceStream = null, cameraTimer = null, cameraEpoch = 0, cameraAbort = null;
function stopDeviceCamera(message = '') {
  cameraEpoch += 1; clearTimeout(cameraTimer); cameraAbort?.abort(); cameraAbort = null;
  deviceStream?.getTracks().forEach(track => track.stop()); deviceStream = null;
  $('device-camera-video').srcObject = null; $('device-camera-video').hidden = true;
  $('device-camera-start').disabled = false; $('device-camera-stop').disabled = true; $('device-camera-facing').disabled = false;
  if (message) { text('test-message', message); $('test-message').hidden = false; }
}
function chooseTestPhoto(file) {
  stopDeviceCamera(); selectedTestPhoto = file || null;
  if (testPreviewUrl) URL.revokeObjectURL(testPreviewUrl);
  testPreviewUrl = null; $('test-preview').hidden = true;
  $('test-results').replaceChildren(); $('test-message').hidden = true; $('test-score-help').hidden = true;
  $('test-submit').disabled = !file;
  if (!file) return;
  if (file.size > 8 * 1024 * 1024) {
    text('test-message', 'Choose a photo smaller than 8 MB.'); $('test-message').hidden = false;
    $('test-submit').disabled = true; return;
  }
  testPreviewUrl = URL.createObjectURL(file); $('test-preview').src = testPreviewUrl; $('test-preview').hidden = false;
}
$('test-file').addEventListener('change', event => chooseTestPhoto(event.target.files[0]));
$('test-camera-photo').addEventListener('change', event => {
  $('test-file').value = ''; chooseTestPhoto(event.target.files[0]);
});
async function sendRecognitionTest(file, signal = AbortSignal.timeout(60000)) {
  const form = new FormData(); form.append('file', file, file.name || 'device-camera.jpg');
  const response = await fetch('/api/faces/test', {method:'POST', body:form, signal});
  const result = await response.json();
  if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : 'This photo could not be tested. Try a clearer image.');
  return result;
}
function renderTestResults(result, live = false) {
  $('test-message').hidden = false; $('test-message').classList.remove('error');
  const prefix = live ? `Live check ${new Date().toLocaleTimeString()} · ` : '';
  text('test-message', prefix + (result.faces.length ? `${result.faces.length} face${result.faces.length === 1 ? '' : 's'} checked${result.low_quality_faces ? ` · ${result.low_quality_faces} small or blurry face(s) skipped` : ''}. Nothing was saved.` : 'No usable faces found. Move closer or improve lighting. Nothing was saved.'));
  $('test-results').replaceChildren(...result.faces.map(face => {
    const card = document.createElement('article'); card.className = 'test-result ' + (face.matched ? 'matched' : 'unmatched');
    const image = document.createElement('img'); image.src = face.image_data_url; image.alt = 'Face found in your test image';
    const info = document.createElement('div');
    const name = document.createElement('h4'); name.textContent = face.matched ? face.name : 'Unknown';
    const label = document.createElement('p'); label.textContent = face.matched ? 'Matched a saved profile' : 'No confident profile match';
    const score = document.createElement('p'); score.className = 'test-score'; score.textContent = typeof face.similarity === 'number' ? `Similarity ${face.similarity.toFixed(3)} · threshold ${result.match_threshold.toFixed(2)}` : 'No saved profiles to compare';
    info.append(name, label, score); card.append(image, info); return card;
  }));
  $('test-score-help').hidden = !result.faces.length;
}
$('test-form').addEventListener('submit', async event => {
  event.preventDefault(); if (testBusy) return;
  const file = selectedTestPhoto; if (!file || file.size > 8 * 1024 * 1024) return;
  stopDeviceCamera(); testBusy = true; $('test-submit').disabled = true; $('test-file').disabled = true; $('test-camera-photo').disabled = true; $('device-camera-start').disabled = true;
  $('test-results').replaceChildren(); $('test-score-help').hidden = true;
  $('test-message').hidden = false; $('test-message').classList.remove('error'); text('test-message', 'Checking the photo against your saved profiles…');
  try {
    renderTestResults(await sendRecognitionTest(file));
  } catch(error) {
    $('test-message').classList.add('error'); text('test-message', error.name === 'TimeoutError' ? 'Test timed out. Please try again.' : error.message);
  } finally { testBusy = false; $('test-submit').disabled = false; $('test-file').disabled = false; $('test-camera-photo').disabled = false; $('device-camera-start').disabled = false; }
});

const localCameraUrl = `http://localhost:${location.port || '8000'}/#faces`;
$('camera-local-link').href = localCameraUrl;
if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) $('device-camera-requirement').hidden = false;
async function checkDeviceFrame(epoch) {
  if (epoch !== cameraEpoch || !deviceStream || document.hidden) return;
  try {
    const video = $('device-camera-video');
    if (!video.videoWidth || video.readyState < 2) {
      cameraTimer = setTimeout(() => checkDeviceFrame(epoch), 500); return;
    }
    const canvas = document.createElement('canvas');
    const scale = Math.min(1, 640 / video.videoWidth, 640 / video.videoHeight);
    canvas.width = Math.round(video.videoWidth * scale); canvas.height = Math.round(video.videoHeight * scale);
    canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', .9));
    if (epoch !== cameraEpoch || !blob) return;
    cameraAbort = new AbortController();
    const timeout = setTimeout(() => cameraAbort?.abort(), 15000);
    try {
      const result = await sendRecognitionTest(blob, cameraAbort.signal);
      if (epoch === cameraEpoch) renderTestResults(result, true);
    } finally { clearTimeout(timeout); }
  } catch(error) {
    if (epoch === cameraEpoch) {
      $('test-message').hidden = false; $('test-message').classList.add('error');
      text('test-message', error.name === 'AbortError' ? 'Live check timed out. Retrying…' : error.message);
    }
  }
  if (epoch === cameraEpoch && deviceStream) cameraTimer = setTimeout(() => checkDeviceFrame(epoch), 2000);
}
$('device-camera-start').addEventListener('click', async () => {
  if (testBusy || deviceStream) return;
  $('test-message').classList.remove('error'); $('test-message').hidden = false;
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    $('device-camera-requirement').hidden = false;
    text('test-message', 'This browser requires HTTPS for live camera access. Use localhost on the laptop, or Take photo on your phone.'); return;
  }
  const epoch = ++cameraEpoch;
  $('device-camera-start').disabled = true; $('device-camera-stop').disabled = false; $('device-camera-facing').disabled = true;
  text('test-message', 'Allow camera access in your browser to begin.');
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio:false, video:{facingMode:{ideal:$('device-camera-facing').value}, width:{ideal:640}, height:{ideal:480}, frameRate:{ideal:15,max:24}}});
    if (epoch !== cameraEpoch) { stream.getTracks().forEach(track => track.stop()); return; }
    deviceStream = stream;
    stream.getTracks().forEach(track => track.addEventListener('ended', () => { if (epoch === cameraEpoch) stopDeviceCamera('Device camera stopped.'); }));
    $('device-camera-video').srcObject = stream; $('device-camera-video').hidden = false; $('test-preview').hidden = true;
    await $('device-camera-video').play();
    if (epoch === cameraEpoch) checkDeviceFrame(epoch);
  } catch(error) {
    if (epoch !== cameraEpoch) return;
    stopDeviceCamera(); $('test-message').hidden = false; $('test-message').classList.add('error');
    text('test-message', error.name === 'NotAllowedError' ? 'Camera permission was denied. Allow camera access in browser settings, or upload a photo.' : error.name === 'NotFoundError' ? 'No camera was found on this device. You can upload a photo instead.' : 'Could not open this device’s camera. Check that it is available and not in use by another app.');
  }
});
$('device-camera-stop').addEventListener('click', () => stopDeviceCamera('Device camera stopped. Test frames were not saved.'));
document.addEventListener('visibilitychange', () => { if (document.hidden && deviceStream) stopDeviceCamera('Device camera stopped while the page was hidden.'); });
window.addEventListener('pagehide', () => stopDeviceCamera());

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
