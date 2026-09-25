/* ── Unified TTS — Frontend ─────────────────────────────────────────── */

// Engine ids are discovered from the backend; this is only the fallback
// order used before /api/engines responds.
let ENGINE_IDS = ['piper', 'kokoro', 'audio8', 'edge-tts', 'gtts'];

// ── State ──────────────────────────────────────────────────────────────
let currentEngine = 'piper';
let currentFilename = null;
let currentUrl = null;
let audioContext = null;
let analyser = null;
let animationId = null;
let isDark = true;
let historyPage = 0;
let switchToken = 0;
let engineInfo = {};   // { engineId: {voices[], is_online, voice_count, error, cap, params, ...} }
let canRegenerate = false;
let downloadPollTimer = null;
let apiKey = '';
let lastFocused = {};

// ── DOM refs ───────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

// ── Init ───────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  consumeApiKeyFromUrl();
  initTheme();
  await Promise.all([loadEngines(), loadSystem()]);
  renderEngineTabs();
  setupModeTabs();
  setupTextControls();
  setupControls();
  setupActions();
  setupHistory();
  setupRegister();
  setupRecent();
  setupModelPanel();
  setupShortcuts();
  switchEngine(currentEngine);
});

// ── API helpers ────────────────────────────────────────────────────────
// Read a one-time key from the URL, scrub it before any request, and keep it
// only in memory/sessionStorage. Never persist credentials in localStorage.
function consumeApiKeyFromUrl() {
  try {
    const url = new URL(window.location.href);
    const fromUrl = url.searchParams.get('api_key');
    if (fromUrl) {
      apiKey = fromUrl;
      sessionStorage.setItem('tts-api-key', fromUrl);
      url.searchParams.delete('api_key');
      window.history.replaceState({}, document.title, url.pathname + url.search + url.hash);
    } else {
      apiKey = sessionStorage.getItem('tts-api-key') || '';
    }
  } catch {
    apiKey = '';
  }
}

function apiKeyHeaders() {
  return apiKey ? { 'X-API-Key': apiKey } : {};
}

async function apiFetch(path, opts = {}) {
  const { headers: optHeaders, ...rest } = opts;
  const res = await fetch(path, {
    ...rest,
    headers: { 'Content-Type': 'application/json', ...apiKeyHeaders(), ...(optHeaders || {}) },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}

// ── Formatting helpers ─────────────────────────────────────────────────
function formatDuration(s) {
  const n = Number(s);
  if (!isFinite(n)) return '—';
  return (Math.round(n * 100) / 100) + 's';
}

function setInlineError(id, message) {
  const node = $(id);
  if (!node) return;
  node.textContent = message || '';
  node.hidden = !message;
}

function openDialog(modalId, overlayId) {
  const modal = $(modalId);
  const overlay = $(overlayId);
  if (!modal) return;
  lastFocused[modalId] = document.activeElement;
  modal.hidden = false;
  modal.setAttribute('aria-hidden', 'false');
  if (overlay) overlay.classList.add('open');
  modal.classList.add('open');
  setTimeout(() => modal.focus(), 0);
}

function closeDialog(modalId, overlayId) {
  const modal = $(modalId);
  const overlay = $(overlayId);
  if (modal) {
    modal.classList.remove('open');
    modal.hidden = true;
    modal.setAttribute('aria-hidden', 'true');
  }
  if (overlay) overlay.classList.remove('open');
  const restore = lastFocused[modalId];
  delete lastFocused[modalId];
  if (restore && typeof restore.focus === 'function') restore.focus();
}

function handleDialogKeys(event) {
  const dialogs = [
    ['modelsModal', 'modelsOverlay', closeModelsModal],
    ['registerModal', 'registerOverlay', closeRegister],
    ['historyPanel', 'historyOverlay', closeHistory],
  ];
  const active = dialogs.find(([modalId]) => $(modalId)?.classList.contains('open'));
  if (!active) return;
  const [modalId, , close] = active;
  if (event.key === 'Escape') {
    event.preventDefault();
    close();
    return;
  }
  if (event.key !== 'Tab') return;
  const focusable = [...$(modalId).querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), audio, [tabindex]:not([tabindex="-1"])')].filter(el => el.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

document.addEventListener('keydown', handleDialogKeys);

// ── Theme ──────────────────────────────────────────────────────────────
// Persisted in localStorage; an inline <head> script applies it before
// first paint so there is no dark→light flash on reload.
function initTheme() {
  isDark = document.documentElement.getAttribute('data-theme') !== 'light';
  applyThemeIcon();
}

function applyThemeIcon() {
  const icon = document.querySelector('.theme-icon');
  if (icon) {
    icon.innerHTML = isDark
      ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></svg>'
      : '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
  }
  $('modeBtn').setAttribute('aria-label', isDark ? 'Switch to light theme' : 'Switch to dark theme');
}

// ── Keyboard shortcuts ─────────────────────────────────────────────────
function setupShortcuts() {
  const gen = $('text');
  gen.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); doGenerate(); }
  });
  const batch = $('batchText');
  batch.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); doBatchGenerate(); }
  });
}

// ── System info & model capability ────────────────────────────────────
// /api/device reports the hardware AND, per engine, whether this machine
// can actually run the model (RAM/VRAM available vs. requirement).
async function loadSystem() {
  try {
    const d = await apiFetch('/api/device');
    const ramTotal = d.ram && d.ram.total_mb ? (d.ram.total_mb / 1024).toFixed(1) : '?';
    let badge = `CPU · ${ramTotal} GB RAM`;
    if (d.gpu && d.gpu.name) {
      badge = `GPU: ${d.gpu.name.slice(0, 18)} · ${(d.gpu.vram_total_mb / 1024).toFixed(0)} GB`;
    }
    $('deviceBadge').textContent = badge;
    const freeRam = d.ram && d.ram.available_mb ? (d.ram.available_mb / 1024).toFixed(1) : '?';
    let tip = `${d.device} · ${freeRam}/${ramTotal} GB RAM free`;
    if (d.gpu && d.gpu.name) tip += ` · ${(d.gpu.vram_available_mb / 1024).toFixed(1)}/${(d.gpu.vram_total_mb / 1024).toFixed(0)} GB VRAM free`;
    tip += ` · auto-unload idle: ${Math.round((d.idle_unload_seconds || 300) / 60)} min`;
    $('deviceBadge').title = tip;
    Object.entries(d.engines || {}).forEach(([eid, cap]) => {
      engineInfo[eid] = { ...(engineInfo[eid] || {}), cap };
    });
  } catch { $('deviceBadge').textContent = 'CPU'; }
}

// ── Engines ────────────────────────────────────────────────────────────
async function loadEngines() {
  try {
    const engines = await apiFetch('/api/engines');
    engines.forEach(e => { engineInfo[e.id] = { ...(engineInfo[e.id] || {}), ...e }; });
    if (engines.length) ENGINE_IDS = engines.map(e => e.id);
  } catch {}
}

function renderEngineTabs() {
  const container = $('engineTabs');
  container.replaceChildren();
  ENGINE_IDS.forEach(eid => {
    const info = engineInfo[eid] || {};
    const btn = document.createElement('button');
    const selected = eid === currentEngine;
    btn.type = 'button';
    btn.className = 'engine-tab' + (selected ? ' active' : '');
    btn.dataset.engine = eid;
    btn.setAttribute('role', 'tab');
    btn.setAttribute('aria-selected', String(selected));
    btn.setAttribute('aria-controls', 'singleSection');
    btn.tabIndex = selected ? 0 : -1;

    let dotClass = '';
    let metaCls = '';
    let meta = 'offline';
    if (info.error) { dotClass = 'error-dot'; meta = 'unavailable'; }
    else if (info.is_online) { dotClass = 'online-dot'; meta = 'online'; }
    else if (info.is_downloadable && !info.installed) { dotClass = 'download-dot'; meta = 'download model'; }
    else if (info.is_downloadable && info.installed) { metaCls = 'cap-ok'; meta = info.voice_count > 0 ? info.voice_count + ' voices' : 'installed'; }
    else if (info.cap && !info.cap.runnable) { metaCls = 'cap-warn'; meta = info.cap.requirement_mb ? 'needs ' + (info.cap.requirement_mb / 1024).toFixed(1) + ' GB RAM' : 'cannot run'; }
    else if (info.voice_count > 0) { metaCls = 'cap-ok'; meta = info.voice_count + ' voices'; }
    else { meta = 'no models'; }

    const top = document.createElement('span');
    top.className = 'engine-tab-top';
    if (dotClass) { const dot = document.createElement('span'); dot.className = dotClass; top.append(dot); }
    const name = document.createElement('span'); name.className = 'engine-tab-name'; name.textContent = eid; top.append(name);
    const status = document.createElement('span'); status.className = 'engine-tab-meta ' + metaCls; status.textContent = meta;
    btn.append(top, status);
    btn.addEventListener('click', () => switchEngine(eid));
    btn.addEventListener('keydown', e => moveEngineTab(e, btn));
    container.appendChild(btn);
  });
}

function moveEngineTab(event, current) {
  const buttons = [...$('engineTabs').querySelectorAll('[role="tab"]')];
  const index = buttons.indexOf(current);
  let next = index;
  if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % buttons.length;
  else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index - 1 + buttons.length) % buttons.length;
  else if (event.key === 'Home') next = 0;
  else if (event.key === 'End') next = buttons.length - 1;
  else return;
  event.preventDefault();
  buttons[next].focus();
  switchEngine(buttons[next].dataset.engine);
}

async function switchEngine(eid) {
  currentEngine = eid;
  const token = ++switchToken;

  // Immediately reset the voice select — never show another engine's voices
  const sel = $('voice');
  sel.innerHTML = '<option value="">Loading voices…</option>';
  $('onlineIndicator').innerHTML = '';
  $('registerVoiceBtn').style.display = 'none';
  $('previewVoiceBtn').style.display = 'none';
  renderEngineTabs();
  updateOutputBadge();

  // "Models" button: only for engines that can download models
  const info0 = engineInfo[eid] || {};
  $('modelsBtn').style.display = info0.is_downloadable ? '' : 'none';

  try {
    const data = await apiFetch('/api/voices?engine_id=' + encodeURIComponent(eid), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ engine_id: eid }),
    });
    if (token !== switchToken) return;
    engineInfo[eid] = {
      ...(engineInfo[eid] || {}),
      voices: data.voices || [],
      is_online: !!data.is_online,
      error: null,
      voice_count: (data.voices || []).length,
    };
    populateVoices(data.voices || []);
    $('onlineIndicator').innerHTML = data.is_online
      ? '<span>● online</span>'
      : '';
  } catch (e) {
    if (token !== switchToken) return;
    engineInfo[eid] = { ...(engineInfo[eid] || {}), voices: [], error: 'unavailable' };
    populateVoices([]);
    $('onlineIndicator').innerHTML = '';
    setStatus('err', `Engine ${eid} unavailable`);
  }
  if (token !== switchToken) return;
  renderEngineTabs();
  updateOutputBadge();
  // Preview available for online engines + audio8; register only for audio8
  $('previewVoiceBtn').style.display = ['edge-tts', 'gtts', 'audio8'].includes(eid) ? '' : 'none';
  $('registerVoiceBtn').style.display = eid === 'audio8' ? '' : 'none';
  showModelPanel(eid);
  renderEngineParams(eid);
}

// ── Model download panel ───────────────────────────────────────────────
// Piper shows the panel whenever its tab is selected (so you can add more
// voices). Kokoro/Audio8 show it only when the model is missing. The
// "Models" button opens the full catalog dialog instead.
let modelsModalTimer = null;
let lastModelsStatus = {};

function setupModelPanel() {
  $('downloadModelBtn').addEventListener('click', startDownload);
  $('modelsBtn').addEventListener('click', openModelsModal);
  $('modelsCloseBtn').addEventListener('click', closeModelsModal);
  $('modelsOverlay').addEventListener('click', closeModelsModal);
}

function openModelsModal() {
  setInlineError('modelsError', '');
  openDialog('modelsModal', 'modelsOverlay');
  renderModelsList();
}

function closeModelsModal() {
  closeDialog('modelsModal', 'modelsOverlay');
  if (modelsModalTimer) { clearInterval(modelsModalTimer); modelsModalTimer = null; }
}

async function renderModelsList() {
  const list = $('modelsList');
  try {
    const data = await apiFetch('/api/models');
    const models = data.models || [];
    list.innerHTML = '';
    let anyDownloading = false;
    for (const m of models) {
      const st = m.status || {};
      if (st.state === 'downloading') anyDownloading = true;
      // Refresh the main UI when a download we started has finished.
      if (st.state === 'done' && lastModelsStatus[m.engine_id] === 'downloading') {
        switchEngine(currentEngine);
      }
      lastModelsStatus[m.engine_id] = st.state;
      list.appendChild(buildModelRow(m, st));
    }
    if (modelsModalTimer) clearInterval(modelsModalTimer);
    modelsModalTimer = anyDownloading ? setInterval(renderModelsList, 1000) : null;
  } catch (e) {
    setInlineError('modelsError', 'Could not load models: ' + e.message);
  }
}

function buildModelRow(m, st) {
  const row = document.createElement('div');
  row.className = 'models-row' + (m.is_online ? ' models-row--online' : '');

  const info = document.createElement('div');
  info.className = 'models-row-info';
  const title = document.createElement('div');
  title.className = 'models-row-title';
  title.textContent = m.label;
  const sub = document.createElement('div');
  sub.className = 'models-row-sub';
  if (m.is_online) {
    sub.textContent = 'Works immediately — requires internet connection';
  } else {
    sub.textContent = m.engine_id === 'piper'
      ? `${m.installed_voices.length} of ${m.voices.length} voices installed · ${m.size_label}`
      : `${m.installed ? 'Installed' : 'Not installed'} · ${m.size_label}`;
  }
  info.append(title, sub);
  row.appendChild(info);

  if (st.state === 'downloading') {
    const bar = document.createElement('div');
    bar.className = 'progress-track';
    const fill = document.createElement('div');
    fill.className = 'progress-fill';
    fill.style.width = Math.max(0, Math.min(100, st.percent || 0)) + '%';
    bar.appendChild(fill);
    const msg = document.createElement('div');
    msg.className = 'model-panel-status';
    msg.textContent = st.message || 'Downloading…';
    row.append(bar, msg);
  } else if (st.state === 'error') {
    const msg = document.createElement('div');
    msg.className = 'model-panel-status';
    msg.style.color = 'var(--error)';
    msg.textContent = st.error || 'Download failed';
    row.appendChild(msg);
  } else if (st.state === 'done' && !m.is_online) {
    const msg = document.createElement('div');
    msg.className = 'model-panel-status';
    msg.textContent = st.message || 'Installed';
    row.appendChild(msg);
  }

  if (!m.is_online && m.engine_id === 'piper' && st.state !== 'downloading') {
    const sel = document.createElement('select');
    sel.className = 'download-voice';
    sel.title = 'Voice to download';
    m.voices.forEach(v => {
      const opt = document.createElement('option');
      opt.value = v;
      opt.textContent = (m.installed_voices || []).includes(v) ? v + ' (installed)' : v;
      sel.appendChild(opt);
    });
    row.appendChild(sel);
  }

  if (!m.is_online) {
    const btn = document.createElement('button');
    btn.className = 'btn btn-primary btn-small';
    btn.textContent = st.state === 'downloading' ? '…' : 'Download';
    btn.disabled = st.state === 'downloading';
    btn.addEventListener('click', async () => {
      const voice = (row.querySelector('.download-voice') || {}).value || '';
      btn.disabled = true;
      btn.textContent = '…';
      try {
        await apiFetch('/api/models/download', {
          method: 'POST',
          body: JSON.stringify({ engine_id: m.engine_id, voice }),
        });
        lastModelsStatus[m.engine_id] = 'downloading';
        renderModelsList();
      } catch (e) {
        btn.disabled = false;
        btn.textContent = 'Download';
        setStatus('err', e.message);
      }
    });
    row.appendChild(btn);
  }
  return row;
}

async function showModelPanel(eid) {
  const panel = $('modelPanel');
  const info = engineInfo[eid];
  const needsModel = info && info.is_downloadable && (eid === 'piper' || !info.installed);
  if (downloadPollTimer) clearInterval(downloadPollTimer); // stale poll
  if (!needsModel) { panel.style.display = 'none'; return; }
  panel.style.display = '';
  $('downloadProgress').style.display = 'none';
  $('downloadStatus').textContent = '';
  $('downloadVoice').style.display = 'none';
  $('downloadModelBtn').disabled = false;
  try {
    const data = await apiFetch('/api/models');
    const spec = (data.models || []).find(m => m.engine_id === eid);
    if (!spec) { panel.style.display = 'none'; return; }
    $('modelTitle').textContent = spec.label;
    const have = (spec.installed_voices || []).length;
    const sub = eid === 'piper'
      ? `${have} of ${spec.voices.length} voices installed · ${spec.size_label}`
      : `${spec.installed ? 'Installed' : 'Not installed'} · ${spec.size_label}`;
    $('modelSub').textContent = sub;
    if (eid === 'piper' && spec.voices && spec.voices.length) {
      const sel = $('downloadVoice');
      sel.innerHTML = '';
      spec.voices.forEach(v => {
        const opt = document.createElement('option');
        opt.value = v;
        opt.textContent = (spec.installed_voices || []).includes(v) ? v + ' (installed)' : v;
        sel.appendChild(opt);
      });
      sel.style.display = '';
    }
    const st = spec.status || {};
    if (st.state === 'downloading') {
      $('downloadModelBtn').disabled = true;
      startDownloadPoll(eid);
    } else if (st.state === 'error') {
      $('downloadStatus').textContent = st.error || 'Download failed';
    } else if (st.state === 'done') {
      $('downloadStatus').textContent = st.message || 'Model installed';
    }
  } catch { /* panel hides itself on failure */ }
}

async function startDownload() {
  const eid = currentEngine;
  const voice = $('downloadVoice').value || '';
  $('downloadModelBtn').disabled = true;
  try {
    await apiFetch('/api/models/download', {
      method: 'POST',
      body: JSON.stringify({ engine_id: eid, voice }),
    });
    $('modelSub').textContent = 'Downloading — keep this tab open…';
    startDownloadPoll(eid);
  } catch (e) {
    $('downloadModelBtn').disabled = false;
    $('downloadStatus').textContent = e.message;
    setStatus('err', e.message);
  }
}

function startDownloadPoll(eid) {
  if (downloadPollTimer) clearInterval(downloadPollTimer);
  $('downloadProgress').style.display = '';
  downloadPollTimer = setInterval(async () => {
    try {
      const st = await apiFetch('/api/models/download/status?engine_id=' + encodeURIComponent(eid));
      $('downloadFill').style.width = Math.max(0, Math.min(100, st.percent || 0)) + '%';
      $('downloadStatus').textContent = st.state === 'error'
        ? (st.error || 'Download failed')
        : (st.message || '');
      if (st.state === 'done') {
        clearInterval(downloadPollTimer);
        downloadPollTimer = null;
        $('downloadModelBtn').disabled = false;
        $('downloadStatus').textContent = 'Model installed — reloading voices…';
        setStatus('ok', 'Model installed');
        switchEngine(eid);
      } else if (st.state === 'error') {
        clearInterval(downloadPollTimer);
        downloadPollTimer = null;
        $('downloadModelBtn').disabled = false;
        $('downloadProgress').style.display = 'none';
        setStatus('err', st.error || 'Download failed');
      }
    } catch { /* keep polling */ }
  }, 1000);
}

function updateOutputBadge() {
  $('outputEngineBadge').textContent = currentEngine;
}

function populateVoices(voices) {
  const sel = $('voice');
  sel.innerHTML = '<option value="">— default —</option>';
  voices.forEach(v => {
    const opt = document.createElement('option');
    opt.value = v;
    opt.textContent = v;
    sel.appendChild(opt);
  });
  if (voices.length) setStatus('ok', `${currentEngine}: ${voices.length} voices`);
}

// ── Dynamic engine params ──────────────────────────────────────────────
const _engineParamsState = {};  // { engineId: { paramName: value } }

function _paramSliderFill(spec, val) {
  const min = spec.min ?? 0;
  const max = spec.max ?? 1;
  const v = val === null || val === undefined ? min : val;
  const pct = max > min ? ((v - min) / (max - min)) * 100 : 0;
  return Math.max(0, Math.min(100, pct));
}

function _paramRowHtml(name, spec, val) {
  const display = (val === null || val === undefined) ? 'auto' : val;
  let control = '';
  if (spec.type === 'bool') {
    control = `<label class="param-toggle">
      <input type="checkbox" data-param="${escHtml(name)}" ${val ? 'checked' : ''}>
      <span class="toggle-track"><span class="toggle-thumb"></span></span>
      <span class="toggle-label">${escHtml(spec.label || name)}</span>
    </label>`;
  } else if (spec.type === 'str' && spec.options) {
    control = `<select data-param="${escHtml(name)}">
      ${spec.options.map(o => `<option value="${escHtml(String(o))}" ${String(o) === String(val) ? 'selected' : ''}>${escHtml(String(o))}</option>`).join('')}
    </select>`;
  } else {
    const step = spec.step ?? (spec.type === 'int' ? 1 : 0.01);
    // Null-default numeric params (e.g. Piper speaker_id = "auto") render the
    // slider at its min bound but display "auto" until the user moves it.
    const sliderVal = (val === null || val === undefined) ? (spec.min ?? 0) : val;
    const fill = _paramSliderFill(spec, val);
    control = `<div class="param-slider-group">
      <input type="range" min="${spec.min ?? 0}" max="${spec.max ?? 1}" step="${step}" value="${sliderVal}" data-param="${escHtml(name)}" style="--fill:${fill}%" data-is-default-null="${val === null || val === undefined ? '1' : ''}">
      <span class="param-val" data-param-val="${escHtml(name)}">${typeof display === 'number' ? (spec.type === 'int' ? String(Math.round(display)) : display.toFixed(2)) : String(display)}</span>
    </div>`;
  }
  return `<div class="param-row" title="${escHtml(spec.help || '')}">
    <span class="param-label">${escHtml(spec.label || name)}</span>
    ${control}
  </div>`;
}

function renderEngineParams(eid) {
  const container = $('engineParamsContainer');
  const params = (engineInfo[eid] && engineInfo[eid].params) || {};
  const entries = Object.entries(params);
  if (!entries.length) {
    container.innerHTML = '';
    container.style.display = 'none';
    return;
  }
  container.style.display = '';
  // Preserve current values for known params
  if (!_engineParamsState[eid]) _engineParamsState[eid] = {};
  const state = _engineParamsState[eid];

  entries.forEach(([name, spec]) => {
    if (state[name] === undefined) state[name] = spec.default ?? (spec.type === 'bool' ? false : null);
  });

  // Main-group params render inline; "advanced" ones collapse into a
  // <details> so the common path stays uncluttered.
  const mainEntries = entries.filter(([, s]) => s.group !== 'advanced');
  const advEntries = entries.filter(([, s]) => s.group === 'advanced');

  let html = mainEntries.map(([n, s]) => _paramRowHtml(n, s, state[n])).join('');
  if (advEntries.length) {
    html += `<details class="param-advanced">
      <summary>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
        Advanced options
      </summary>
      <div class="param-advanced-body">
        ${advEntries.map(([n, s]) => _paramRowHtml(n, s, state[n])).join('')}
      </div>
    </details>`;
  }
  container.innerHTML = html;

  const updateValSpan = (n, v, spec) => {
    const valSpan = container.querySelector(`.param-val[data-param-val="${n}"]`);
    if (valSpan) {
      valSpan.textContent = (v === null || v === undefined) ? 'auto'
        : (spec.type === 'int' ? String(Math.round(v)) : v.toFixed(2));
    }
  };

  // Attach event listeners
  container.querySelectorAll('input[data-param], select[data-param]').forEach(el => {
    el.addEventListener('change', () => {
      const n = el.dataset.param;
      const spec = params[n];
      if (!spec) return;
      if (spec.type === 'bool') {
        _engineParamsState[eid][n] = el.checked;
      } else if (spec.type === 'int') {
        _engineParamsState[eid][n] = parseInt(el.value, 10);
      } else {
        _engineParamsState[eid][n] = parseFloat(el.value);
      }
      if (el.type === 'range') {
        el.style.setProperty('--fill', _paramSliderFill(spec, _engineParamsState[eid][n]) + '%');
        el.removeAttribute('data-is-default-null');
      }
      updateValSpan(n, _engineParamsState[eid][n], spec);
    });
  });
}

function getEngineParams(eid) {
  const params = (engineInfo[eid] && engineInfo[eid].params) || {};
  const state = _engineParamsState[eid] || {};
  const out = {};
  Object.keys(params).forEach(name => {
    if (state[name] !== undefined && state[name] !== params[name].default) {
      out[name] = state[name];
    }
  });
  return out;
}

// ── Mode tabs ──────────────────────────────────────────────────────────
function setupModeTabs() {
  const tabs = [...document.querySelectorAll('.mode-tab')];
  const selectMode = (mode, focus = false) => {
    tabs.forEach(tab => {
      const selected = tab.dataset.mode === mode;
      tab.classList.toggle('active', selected);
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      if (selected && focus) tab.focus();
    });
    $('singleSection').style.display = mode === 'single' ? '' : 'none';
    $('batchSection').style.display = mode === 'batch' ? '' : 'none';
    $('generateBtn').style.display = mode === 'single' ? '' : 'none';
    $('regenerateBtn').style.display = mode === 'single' ? '' : 'none';
    $('batchGenerateBtn').style.display = mode === 'batch' ? '' : 'none';
  };
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectMode(tab.dataset.mode));
    tab.addEventListener('keydown', event => {
      let next = index;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % tabs.length;
      else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = tabs.length - 1;
      else return;
      event.preventDefault();
      selectMode(tabs[next].dataset.mode, true);
    });
  });
}

// ── Text controls ──────────────────────────────────────────────────────
function setupTextControls() {
  $('text').addEventListener('input', () => {
    $('charCount').textContent = `${$('text').value.length} / 5000`;
  });
  $('batchText').addEventListener('input', () => {
    $('batchCharCount').textContent = `${$('batchText').value.length} / 50000`;
  });
}

// ── Sliders ────────────────────────────────────────────────────────────
function setupControls() {
  const speed = $('speed'), pitch = $('pitch');
  const updateFill = (el, min, max) => {
    const pct = ((parseFloat(el.value) - min) / (max - min)) * 100;
    el.style.setProperty('--fill', pct + '%');
  };
  updateFill(speed, 0.5, 2.0);
  updateFill(pitch, -12, 12);
  speed.addEventListener('input', e => {
    $('speedVal').textContent = parseFloat(e.target.value).toFixed(1) + 'x';
    updateFill(e.target, 0.5, 2.0);
  });
  pitch.addEventListener('input', e => {
    $('pitchVal').textContent = e.target.value;
    updateFill(e.target, -12, 12);
  });
}

// ── Actions ────────────────────────────────────────────────────────────
function setupActions() {
  $('generateBtn').addEventListener('click', doGenerate);
  $('regenerateBtn').addEventListener('click', doRegenerate);
  $('batchGenerateBtn').addEventListener('click', doBatchGenerate);
  $('playBtn').addEventListener('click', playCurrent);
  $('saveBtn').addEventListener('click', saveCurrent);
  $('clearBtn').addEventListener('click', clearAll);
  $('previewVoiceBtn').addEventListener('click', previewVoice);
}

async function doGenerate() {
  const text = $('text').value.trim();
  if (!text) { setStatus('warn', 'Please enter some text'); return; }
  setLoading(true);
  try {
    const data = await apiFetch('/api/generate', {
      method: 'POST',
      body: JSON.stringify({
        text,
        engine_id: currentEngine,
        voice: $('voice').value,
        speed: parseFloat($('speed').value),
        pitch: parseInt($('pitch').value),
        fmt: $('format').value,
        params: getEngineParams(currentEngine),
      }),
    });
    currentFilename = data.filename;
    currentUrl = data.url;
    canRegenerate = true;
    $('regenerateBtn').disabled = false;
    $('audioPlayer').style.display = '';
    $('audio').src = data.url + '?t=' + Date.now();
    $('playBtn').disabled = false;
    $('saveBtn').disabled = false;
    $('audioMeta').textContent = `${currentEngine} · ${$('voice').value || 'default'} · ${formatDuration(data.duration)}`;
    drawVisualizer(data.url);
    updateOutputBadge();
    loadRecent();
    setStatus('ok', `Generated · ${formatDuration(data.duration)}`);
  } catch (e) {
    setStatus('err', e.message);
  }
  setLoading(false);
}

// ── In-line regeneration ───────────────────────────────────────────────
// Re-runs the last generation with the currently selected voice/params,
// replacing the audio in the player in place.
async function doRegenerate() {
  if (!canRegenerate) { setStatus('warn', 'Generate something first'); return; }
  setLoading(true);
  try {
    const data = await apiFetch('/api/regenerate', {
      method: 'POST',
      body: JSON.stringify({
        voice: $('voice').value,
        speed: parseFloat($('speed').value),
        pitch: parseInt($('pitch').value),
        fmt: $('format').value,
        params: getEngineParams(currentEngine),
      }),
    });
    currentFilename = data.filename;
    currentUrl = data.url;
    $('audioPlayer').style.display = '';
    $('audio').src = data.url + '?t=' + Date.now();
    $('playBtn').disabled = false;
    $('saveBtn').disabled = false;
    $('audioMeta').textContent = `${currentEngine} · ${$('voice').value || 'default'} · ${formatDuration(data.duration)}`;
    drawVisualizer(data.url);
    updateOutputBadge();
    loadRecent();
    setStatus('ok', `Regenerated · ${formatDuration(data.duration)}`);
  } catch (e) {
    setStatus('err', e.message);
  }
  setLoading(false);
}

async function doBatchGenerate() {
  const lines = $('batchText').value.split('\n').filter(l => l.trim());
  if (!lines.length) { setStatus('warn', 'Please enter text lines'); return; }
  setLoading(true);
  setStatus('ok', `Generating ${lines.length} items…`);
  const wrap = $('batchProgressWrap');
  const fill = $('batchProgressFill');
  const label = $('batchProgressLabel');
  wrap.style.display = '';
  wrap.classList.add('is-indeterminate');
  const track = wrap.querySelector('.batch-progress-track');
  track.removeAttribute('aria-valuenow');
  track.setAttribute('aria-valuetext', `Working on ${lines.length} items`);
  fill.style.width = '32%';
  label.textContent = `Working… ${lines.length} items`;
  try {
    const resp = await fetch('/api/generate-batch-stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...apiKeyHeaders() },
      body: JSON.stringify({
        texts: lines,
        engine_id: currentEngine,
        voice: $('voice').value,
        speed: parseFloat($('speed').value),
        pitch: parseInt($('pitch').value),
        fmt: $('format').value,
        params: getEngineParams(currentEngine),
      }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const failed = parseInt(resp.headers.get('X-Batch-Failed') || '0', 10);
    const dropped = parseInt(resp.headers.get('X-Batch-Dropped') || '0', 10);
    const url = URL.createObjectURL(await resp.blob());
    const a = document.createElement('a');
    a.href = url; a.download = 'batch.zip'; a.click();
    URL.revokeObjectURL(url);
    let msg = 'Batch complete · downloaded';
    const notes = [];
    if (failed) notes.push(`${failed} item${failed > 1 ? 's' : ''} failed (see _manifest.json)`);
    if (dropped) notes.push(`${dropped} dropped over the 100-item limit`);
    if (notes.length) { msg += ' — ' + notes.join(', '); setStatus('warn', msg); }
    else setStatus('ok', msg);
  } catch (e) {
    setStatus('err', e.message);
  }
  wrap.classList.remove('is-indeterminate');
  wrap.style.display = 'none';
  setLoading(false);
}

async function previewVoice() {
  const voice = $('voice').value;
  if (!voice) { setStatus('warn', 'Select a voice first'); return; }
  setLoading(true);
  try {
    const data = await apiFetch(
      `/api/audio-preview?engine_id=${encodeURIComponent(currentEngine)}&voice=${encodeURIComponent(voice)}`
    );
    // Play the preview inline in the main player rather than forcing a download
    currentFilename = data.filename;
    currentUrl = data.url;
    $('audioPlayer').style.display = '';
    $('audio').src = data.url + '?t=' + Date.now();
    $('playBtn').disabled = false;
    $('saveBtn').disabled = false;
    $('audioMeta').textContent = `${currentEngine} · ${voice} · preview`;
    $('audio').play().catch(() => {});
    startVisualizer();
    setStatus('ok', 'Preview playing');
  } catch (e) {
    setStatus('err', e.message);
  }
  setLoading(false);
}

function playCurrent() {
  const audio = $('audio');
  if (audio.paused) {
    audio.play();
    startVisualizer();
  } else {
    audio.pause();
    stopVisualizer();
  }
}

function saveCurrent() {
  if (!currentUrl) return;
  const a = document.createElement('a');
  a.href = currentUrl + '?t=' + Date.now();
  a.download = currentFilename;
  a.click();
}

function clearAll() {
  $('text').value = '';
  $('batchText').value = '';
  $('charCount').textContent = '0 / 5000';
  $('batchCharCount').textContent = '0 / 50000';
  currentFilename = null;
  currentUrl = null;
  $('audio').src = '';
  $('audioPlayer').style.display = 'none';
  $('playBtn').disabled = true;
  $('saveBtn').disabled = true;
  $('regenerateBtn').disabled = true;
  canRegenerate = false;
  stopVisualizer();
}

// ── Recent list ────────────────────────────────────────────────────────
function setupRecent() { loadRecent(); }

async function loadRecent() {
  try {
    const data = await apiFetch('/api/history?limit=5');
    renderRecent(data.generations || []);
  } catch {}
}

function renderRecent(items) {
  const list = $('recentList');
  if (!items.length) {
    list.innerHTML = '<div class="recent-empty">No recent generations — generate something!</div>';
    return;
  }
  list.innerHTML = items.map(g => `
    <div class="recent-item" data-fn="${escHtml(g.filename)}" data-url="/output/${escHtml(g.filename)}" data-engine="${escHtml(g.engine)}" title="Play ${escHtml(g.filename)}">
      <span class="recent-engine">${escHtml(g.engine)}</span>
      <span class="recent-text">${escHtml(g.text)}</span>
      <span class="recent-dur">${formatDuration(g.duration)}</span>
      <button class="recent-play" title="Play">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
      </button>
    </div>
  `).join('');
  list.querySelectorAll('.recent-play, .recent-item').forEach(el => {
    el.addEventListener('click', ev => {
      if (el.classList.contains('recent-play')) ev.stopPropagation();
      const item = el.closest('.recent-item');
      loadRecentIntoPlayer(item.dataset.fn, item.dataset.url, item.dataset.engine);
    });
  });
}

function loadRecentIntoPlayer(fn, url, engine) {
  currentFilename = fn;
  currentUrl = url;
  $('audioPlayer').style.display = '';
  $('audio').src = url + '?t=' + Date.now();
  $('playBtn').disabled = false;
  $('saveBtn').disabled = false;
  // Show the engine that produced this clip, not the currently selected tab
  $('audioMeta').textContent = `${engine || currentEngine} · ${fn}`;
  drawVisualizer(url);
  setStatus('ok', 'Loaded from recent');
}

// ── Voice registration (Audio8 custom voices) ─────────────────────────
function setupRegister() {
  $('registerVoiceBtn').addEventListener('click', openRegister);
  $('registerCloseBtn').addEventListener('click', closeRegister);
  $('registerCancelBtn').addEventListener('click', closeRegister);
  $('registerOverlay').addEventListener('click', closeRegister);
  $('registerForm').addEventListener('submit', submitRegister);
}

function openRegister() {
  setInlineError('registerError', '');
  openDialog('registerModal', 'registerOverlay');
  setTimeout(() => $('regName').focus(), 60);
}

function closeRegister() {
  closeDialog('registerModal', 'registerOverlay');
}

async function submitRegister(e) {
  e.preventDefault();
  const name = $('regName').value.trim();
  const text = $('regText').value.trim();
  const file = $('regFile').files[0];
  if (!name || !text || !file) {
    setInlineError('registerError', 'Fill all fields and choose an audio sample.');
    return;
  }
  const fd = new FormData();
  fd.append('name', name);
  fd.append('text', text);
  fd.append('audio', file);
  fd.append('overwrite', $('regOverwrite').checked ? 'true' : 'false');

  const btn = $('registerSubmitBtn');
  btn.disabled = true;
  btn.classList.add('loading');
  setInlineError('registerError', '');
  $('registerModal').setAttribute('aria-busy', 'true');
  setStatus('warn', 'Registering voice — this can take a minute…');
  try {
    const res = await fetch('/api/voices/register', { method: 'POST', body: fd, headers: { ...apiKeyHeaders() } });
    if (!res.ok) throw new Error((await res.text()).slice(0, 300));
    closeRegister();
    setStatus('ok', `Voice "${name}" registered`);
    $('registerForm').reset();
    // Refresh voices and select the newly registered one
    await switchEngine('audio8');
    const sel = $('voice');
    [...sel.options].some(o => { if (o.value === name) { sel.value = name; return true; } return false; });
  } catch (err) {
    setInlineError('registerError', 'Registration failed: ' + err.message);
    setStatus('err', 'Registration failed: ' + err.message);
  } finally {
    $('registerModal').setAttribute('aria-busy', 'false');
    btn.disabled = false;
    btn.classList.remove('loading');
  }
}

// ── Visualizer ─────────────────────────────────────────────────────────
function getVar(name, fallback) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

function setHintVisible(visible) {
  const hint = document.querySelector('.visualizer-hint');
  if (hint) hint.style.display = visible ? '' : 'none';
}

function startVisualizer() {
  setHintVisible(false);
  if (!audioContext) audioContext = new (window.AudioContext || window.webkitAudioContext)();
  if (!analyser) {
    const source = audioContext.createMediaElementSource($('audio'));
    analyser = audioContext.createAnalyser();
    analyser.fftSize = 256;
    source.connect(analyser);
    analyser.connect(audioContext.destination);
  }
  const canvas = $('visualizer');
  const ctx = canvas.getContext('2d');
  canvas.width = canvas.offsetWidth * 2;
  canvas.height = canvas.offsetHeight * 2;
  const bufferLength = analyser.frequencyBinCount;
  const dataArray = new Uint8Array(bufferLength);
  const gradient = ctx.createLinearGradient(0, canvas.height, 0, 0);
  gradient.addColorStop(0, getVar('--viz-bar-a', '#6366f1'));
  gradient.addColorStop(1, getVar('--viz-bar-b', '#8b5cf6'));

  const useRoundRect = typeof ctx.roundRect === 'function';

  function draw() {
    animationId = requestAnimationFrame(draw);
    analyser.getByteFrequencyData(dataArray);
    ctx.fillStyle = getVar('--viz-bg', '#0a0c12');
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    const barW = Math.max(2, Math.floor((canvas.width / bufferLength) * 2.5));
    const gap = Math.max(1, barW / 3);
    const radius = Math.min(barW / 2, 5);
    let x = 0;
    for (let i = 2; i < bufferLength; i++) {
      const v = dataArray[i] / 255;
      const h = Math.pow(v, 1.25) * canvas.height * 0.92;
      if (h <= 0.5) continue;
      // Bottom-up mirrored rounded bars
      ctx.fillStyle = gradient;
      if (useRoundRect) {
        ctx.beginPath();
        ctx.roundRect(x, canvas.height - h, barW - gap, h, radius);
        ctx.fill();
      } else {
        ctx.fillRect(x, canvas.height - h, barW - gap, h);
      }
      // Mirror from the top edge
      ctx.globalAlpha = 0.22;
      if (useRoundRect) {
        ctx.beginPath();
        ctx.roundRect(x, 0, barW - gap, h, radius);
        ctx.fill();
      } else {
        ctx.fillRect(x, 0, barW - gap, h);
      }
      ctx.globalAlpha = 1;
      x += barW;
    }
    // Baseline glow
    ctx.fillStyle = getVar('--viz-glow', 'rgba(99,102,241,0.3)');
    ctx.globalAlpha = 0.5;
    ctx.fillRect(0, canvas.height - 2, canvas.width, 1);
    ctx.globalAlpha = 1;
  }
  draw();
}

function stopVisualizer() {
  if (animationId) { cancelAnimationFrame(animationId); animationId = null; }
  const canvas = $('visualizer');
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!$('audioPlayer').style.display || $('audioPlayer').style.display === 'none') {
    setHintVisible(true);
  }
}

function drawVisualizer(url) {
  stopVisualizer();
  setHintVisible(false);
  const canvas = $('visualizer');
  const ctx = canvas.getContext('2d');
  canvas.width = canvas.offsetWidth * 2;
  canvas.height = canvas.offsetHeight * 2;
  ctx.fillStyle = getVar('--viz-bg', '#0a0c12');
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  // Deterministic pseudo-random envelope → static "peak bars" placeholder
  const gradient = ctx.createLinearGradient(0, canvas.height, 0, 0);
  gradient.addColorStop(0, getVar('--viz-bar-a', '#6366f1'));
  gradient.addColorStop(1, getVar('--viz-bar-b', '#8b5cf6'));
  const useRoundRect = typeof ctx.roundRect === 'function';
  const bars = 48;
  const barW = Math.max(2, Math.floor(canvas.width / bars) * 0.6);
  const gap = Math.max(1, barW / 3);
  const radius = Math.min(barW / 2, 5);
  const mid = canvas.height * 0.5;
  for (let i = 0; i < bars; i++) {
    const amp = 0.35 + 0.5 * Math.abs(Math.sin(i * 2.1 + 1.3)) * Math.abs(Math.cos(i * 0.9));
    const h = amp * canvas.height * 0.55;
    const x = (canvas.width / bars) * i + (canvas.width / bars - barW) / 2;
    ctx.fillStyle = gradient;
    if (useRoundRect) {
      ctx.beginPath();
      ctx.roundRect(x, mid - h, barW - gap, h, radius);
      ctx.fill();
    } else {
      ctx.fillRect(x, mid - h, barW - gap, h);
    }
    ctx.globalAlpha = 0.18;
    if (useRoundRect) {
      ctx.beginPath();
      ctx.roundRect(x, mid, barW - gap, h, radius);
      ctx.fill();
    } else {
      ctx.fillRect(x, mid, barW - gap, h);
    }
    ctx.globalAlpha = 1;
  }
  ctx.fillStyle = getVar('--viz-glow', 'rgba(99,102,241,0.3)');
  ctx.globalAlpha = 0.5;
  ctx.fillRect(0, canvas.height - 2, canvas.width, 1);
  ctx.globalAlpha = 1;
}

// ── Theme toggle ───────────────────────────────────────────────────────
$('modeBtn').addEventListener('click', () => {
  isDark = !isDark;
  document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
  try { localStorage.setItem('tts-theme', isDark ? 'dark' : 'light'); } catch {}
  applyThemeIcon();
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = isDark ? '#0a0a0c' : '#f7f6f3';
  if (currentUrl) drawVisualizer(currentUrl);
});

// ── Status helper ──────────────────────────────────────────────────────
function setStatus(type, msg) {
  const icon = $('statusIcon');
  icon.className = 'status-icon ' + type;
  icon.textContent = type === 'ok' ? '✓' : type === 'err' ? '✕' : '!';
  $('statusMsg').textContent = msg;
}
function setLoading(loading) {
  $('generateBtn').disabled = loading;
  $('batchGenerateBtn').disabled = loading;
  $('regenerateBtn').disabled = loading || !canRegenerate;
  $('main-content').setAttribute('aria-busy', String(loading));
  document.querySelectorAll('#generateBtn, #batchGenerateBtn, #regenerateBtn').forEach(b =>
    b.classList.toggle('loading', loading));
  setStatus(loading ? 'warn' : 'ok', loading ? 'Generating…' : 'Ready');
}

// ── History ────────────────────────────────────────────────────────────
function setupHistory() {
  $('openHistoryBtn').addEventListener('click', () => { historyPage = 0; openHistory(); loadHistory(); });
  $('closeHistoryBtn').addEventListener('click', closeHistory);
  $('historyOverlay').addEventListener('click', closeHistory);
  $('cleanHistoryBtn').addEventListener('click', cleanHistory);
}

async function loadHistory() {
  const list = $('historyList');
  list.setAttribute('aria-busy', 'true');
  setInlineError('historyError', '');
  try {
    const data = await apiFetch(`/api/history?limit=50&offset=${historyPage * 50}`);
    renderHistory(data.generations || []);
  } catch (e) {
    setInlineError('historyError', 'Could not load history: ' + e.message);
    setStatus('err', 'Failed to load history');
  } finally {
    list.setAttribute('aria-busy', 'false');
  }
}

function renderHistory(items) {
  const list = $('historyList');
  list.replaceChildren();
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'history-empty';
    empty.textContent = 'No generations yet — your completed takes will appear here.';
    list.appendChild(empty);
    renderHistoryPagination(0);
    return;
  }
  items.forEach(g => {
    const item = document.createElement('article');
    item.className = 'history-item';
    const top = document.createElement('div'); top.className = 'hi-top';
    const engine = document.createElement('span'); engine.className = 'hi-engine'; engine.textContent = String(g.engine || 'unknown');
    const duration = document.createElement('span'); duration.className = 'hi-duration'; duration.textContent = formatDuration(g.duration) + ' · ' + (g.voice || 'default');
    const text = document.createElement('div'); text.className = 'hi-text'; text.textContent = g.text || ''; text.title = g.text || '';
    const bottom = document.createElement('div'); bottom.className = 'hi-bottom';
    const audio = document.createElement('audio'); audio.controls = true; audio.preload = 'none'; audio.src = '/output/' + encodeURIComponent(g.filename) + '?t=' + Date.now();
    const remove = document.createElement('button'); remove.className = 'hi-delete'; remove.type = 'button'; remove.dataset.fn = g.filename; remove.setAttribute('aria-label', 'Delete this generation'); remove.textContent = '⌫';
    remove.addEventListener('click', () => deleteHistoryItem(remove, g.filename));
    top.append(engine, duration); bottom.append(audio, remove); item.append(top, text, bottom); list.appendChild(item);
  });
  renderHistoryPagination(items.length);
}

function renderHistoryPagination(count) {
  const pager = $('historyPagination');
  pager.replaceChildren();
  const prev = document.createElement('button'); prev.className = 'btn btn-outline btn-small'; prev.type = 'button'; prev.textContent = '← Previous'; prev.disabled = historyPage === 0; prev.addEventListener('click', () => { historyPage = Math.max(0, historyPage - 1); loadHistory(); });
  const next = document.createElement('button'); next.className = 'btn btn-outline btn-small'; next.type = 'button'; next.textContent = 'Next →'; next.disabled = count < 50; next.addEventListener('click', () => { historyPage += 1; loadHistory(); });
  const label = document.createElement('span'); label.className = 'field-help'; label.textContent = 'Page ' + (historyPage + 1);
  pager.append(prev, label, next);
}

async function deleteHistoryItem(button, filename) {
  if (!window.confirm('Delete this generation permanently?')) return;
  button.disabled = true;
  try {
    await apiFetch('/api/history/delete', { method: 'POST', body: JSON.stringify({ filename }) });
    button.closest('.history-item')?.remove();
    setInlineError('historyError', '');
  } catch (e) {
    button.disabled = false;
    setInlineError('historyError', 'Could not delete generation: ' + e.message);
  }
}

function openHistory() {
  setInlineError('historyError', '');
  openDialog('historyPanel', 'historyOverlay');
}
function closeHistory() {
  closeDialog('historyPanel', 'historyOverlay');
}

async function cleanHistory() {
  if (!window.confirm('Delete generations older than 30 days? This cannot be undone.')) return;
  const button = $('cleanHistoryBtn');
  button.disabled = true;
  try {
    await apiFetch('/api/history/clean', { method: 'POST', body: JSON.stringify({ days: 30 }) });
    historyPage = 0;
    await loadHistory();
    setInlineError('historyError', '');
  } catch (e) {
    setInlineError('historyError', 'Could not clean history: ' + e.message);
  } finally {
    button.disabled = false;
  }
}

function escHtml(s) {
  // Escapes &, <, > AND quotes — the quoted forms matter because results are
  // interpolated into double-quoted HTML attributes (title="…", data-fn="…").
  // Without them, text containing `"` could break out of an attribute.
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
