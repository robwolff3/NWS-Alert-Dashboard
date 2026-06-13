#!/usr/bin/env python3
"""Minimal Flask frontend for dsame3 EAS alerts."""
import sys
sys.path.insert(0, '/app/scripts')

import json
import os
import struct
import threading
import time
from pathlib import Path
import html as _html
from flask import Flask, jsonify, request, send_file, abort, Response, stream_with_context
import alerts as alertdb
import push as pushdb

app = Flask(__name__)

# Changes on each container start — forces SW cache refresh after updates
_BOOT = int(time.time())

SITE_TITLE    = os.environ.get('SITE_TITLE',    'NWS Alert Dashboard')
SITE_SUBTITLE = os.environ.get('SITE_SUBTITLE', '')
SITE_FOOTER   = os.environ.get('SITE_FOOTER',   '')

# Web-push UI toggle: when false the dashboard hides the browser-notification
# bar entirely (server still ignores any orphaned subscriptions).
WEB_PUSH_ENABLED = os.environ.get('WEB_PUSH_ENABLED', 'true').strip().lower() \
    not in ('0', 'false', 'no', 'off')

# Radio toggle: when false there is no /tmp/audio_fifo to stream, so the
# Live Radio player is omitted from the page entirely.
RADIO_ENABLED = os.environ.get('RADIO_ENABLED', 'true').strip().lower() \
    not in ('0', 'false', 'no', 'off')

_LIVE_PLAYER_HTML = '''<div class="live-player">
  <div class="live-dot" id="live-dot"></div>
  <span class="live-label">Live Radio</span>
  <audio id="live-audio" controls preload="none">
    <source src="/stream" type="audio/wav">
  </audio>
</div>'''

DERIVED_JSON = '/alerts/derived_config.json'


def _cached_zone_name(ugc):
    """County/zone name from the offline map cache (fallback for the subtitle)."""
    if not ugc:
        return ''
    import config as cfg
    p = Path(cfg.env('MAP_CACHE_DIR', '/alerts/mapdata')) / 'zones' / f'{ugc}.geojson'
    try:
        with open(p) as f:
            return (json.load(f).get('name') or '').strip()
    except (OSError, ValueError):
        return ''


def _resolved_subtitle():
    """SITE_SUBTITLE if set, else the detected location as 'City, County, ST'."""
    if SITE_SUBTITLE:
        return SITE_SUBTITLE
    try:
        with open(DERIVED_JSON) as f:
            info = json.load(f)
    except (OSError, ValueError):
        info = {}
    near       = (info.get('near') or '').strip()
    county_ugc = (info.get('county_ugc') or '').strip()
    state  = county_ugc[:2] if len(county_ugc) >= 2 and county_ugc[:2].isalpha() else ''
    county = (info.get('county_name') or '').strip() or _cached_zone_name(county_ugc)
    if near:
        parts = [near]
        if county:
            parts.append(f'{county} County')
        if state:
            parts.append(state)
        return ', '.join(parts)
    return os.environ.get('LOCATION', '').strip()

_MANIFEST = {
    'name': 'NWS Alert Dashboard',
    'short_name': 'NWS Alerts',
    'description': 'Multi-source NWS alert monitor (NWWS-OI, weather radio, REST API)',
    'start_url': '/',
    'display': 'standalone',
    'background_color': '#0f1117',
    'theme_color': '#0f1117',
    'icons': [
        {'src': '/icons/icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any maskable'},
        {'src': '/icons/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any maskable'},
    ],
}

_SW = f"""\
const CACHE = 'nwr-{_BOOT}';
const STATIC = ['/', '/manifest.json', '/icons/icon-192.png', '/icons/icon-512.png', '/icons/notif-icon.png'];

self.addEventListener('install', e => {{
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(STATIC)).then(() => self.skipWaiting()));
}});

self.addEventListener('activate', e => {{
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
}});

self.addEventListener('fetch', e => {{
  const p = new URL(e.request.url).pathname;
  if (p.startsWith('/api/') || p === '/events' || p === '/stream' ||
      p.startsWith('/audio/') || p.startsWith('/push/')) return;
  e.respondWith(
    caches.match(e.request).then(cached => {{
      if (cached) return cached;
      return fetch(e.request).then(resp => {{
        if (resp.ok) caches.open(CACHE).then(c => c.put(e.request, resp.clone()));
        return resp;
      }});
    }})
  );
}});

self.addEventListener('push', e => {{
  let d = {{}};
  try {{ d = e.data ? e.data.json() : {{}}; }} catch(_) {{}}
  const p = d.priority || 3;
  e.waitUntil(
    self.registration.showNotification(d.title || 'NWR Alert', {{
      body:              d.body || '',
      icon:              '/icons/icon-192.png',
      badge:             '/icons/notif-icon.png',
      tag:               'nwr-' + p,
      renotify:          true,
      requireInteraction: p >= 4,
      data:              {{ url: '/' }}
    }})
  );
}});

self.addEventListener('notificationclick', e => {{
  e.notification.close();
  e.waitUntil(
    clients.matchAll({{type: 'window', includeUncontrolled: true}}).then(list => {{
      for (const c of list) {{ if ('focus' in c) return c.focus(); }}
      return clients.openWindow(e.notification.data?.url || '/');
    }})
  );
}});
"""

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__</title>
<script>(function(){var t=localStorage.getItem('theme');if(t&&t!=='auto')document.documentElement.setAttribute('data-theme',t);})();</script>
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#0f1117">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="NWR Alerts">
<link rel="apple-touch-icon" href="/icons/icon-192.png">
<link rel="stylesheet" href="/static/leaflet/leaflet.css">
<script src="/static/leaflet/leaflet.js"></script>
<style>
:root {
  --bg:      #0f1117;
  --surface: #1a1d26;
  --border:  #2a2d3a;
  --text:    #e2e8f0;
  --muted:   #64748b;
  --p5:    #dc2626; --p5-bg: #2d0a0a;
  --p4:    #ea580c; --p4-bg: #2d1007;
  --p3:    #ca8a04; --p3-bg: #2a1a00;
  --p2:    #2563eb; --p2-bg: #0c1a3e;
  color-scheme: dark;
}
/* Light theme — applied when the user picks it, or by OS preference when they
   haven't (auto). The early inline script sets data-theme for explicit picks. */
:root[data-theme="light"]{
  --bg:#f4f6fa; --surface:#ffffff; --border:#d6dbe4;
  --text:#1a1d26; --muted:#5a6577;
  --p5:#dc2626; --p5-bg:#fde8e8;
  --p4:#ea580c; --p4-bg:#fdebd8;
  --p3:#b8860b; --p3-bg:#fbf3d2;
  --p2:#2563eb; --p2-bg:#dde8fd;
  color-scheme: light;
}
@media (prefers-color-scheme: light){
  :root:not([data-theme]){
    --bg:#f4f6fa; --surface:#ffffff; --border:#d6dbe4;
    --text:#1a1d26; --muted:#5a6577;
    --p5:#dc2626; --p5-bg:#fde8e8;
    --p4:#ea580c; --p4-bg:#fdebd8;
    --p3:#b8860b; --p3-bg:#fbf3d2;
    --p2:#2563eb; --p2-bg:#dde8fd;
    color-scheme: light;
  }
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  font-family:'JetBrains Mono','Fira Code',ui-monospace,monospace;
  background:var(--bg);color:var(--text);
  min-height:100vh;padding:1.5rem;max-width:900px;margin:0 auto;
}
header{
  display:flex;align-items:center;justify-content:space-between;
  margin-bottom:1.5rem;padding-bottom:1rem;border-bottom:1px solid var(--border);
}
.header-brand{display:flex;align-items:center;gap:.9rem}
.nwr-logo{height:52px;width:auto;flex-shrink:0}
.header-title{font-size:1rem;font-weight:700;letter-spacing:.03em;line-height:1.2}
.header-subtitle{font-size:.7rem;color:var(--muted);letter-spacing:.05em;margin-top:.15rem}
.header-right{display:flex;align-items:center;gap:.6rem}
.status-chips{display:flex;flex-wrap:wrap;gap:.3rem;justify-content:flex-end;max-width:14rem}
.theme-toggle{
  background:var(--surface);border:1px solid var(--border);color:var(--text);
  width:1.9rem;height:1.9rem;border-radius:.4rem;font-size:.85rem;line-height:1;
  cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center;
}
.theme-toggle:hover{background:var(--border)}
.updated{
  font-size:.65rem;font-weight:400;color:var(--muted);
  text-transform:none;letter-spacing:normal;
}
.history-controls{display:flex;align-items:center;gap:0}
.ctrl-toggle,.test-btn{
  border-left:1px dashed var(--border);padding-left:.75rem;margin-left:.75rem;
}
.ctrl-toggle{
  font-size:.65rem;font-weight:400;text-transform:none;letter-spacing:normal;
  color:var(--muted);cursor:pointer;display:flex;align-items:center;gap:.3rem;
}
.section-label{
  font-size:.8rem;font-weight:700;letter-spacing:.12em;
  text-transform:uppercase;color:var(--text);
  margin-bottom:.85rem;padding-bottom:.5rem;
  border-bottom:2px solid var(--border);
}
section + section{margin-top:2rem}
.card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:.5rem;padding:1.25rem;margin-bottom:.75rem;
  border-left:3px solid var(--border);
}
.card.p5{border-left-color:var(--p5);background:var(--p5-bg)}
.card.p4{border-left-color:var(--p4);background:var(--p4-bg)}
.card.p3{border-left-color:var(--p3);background:var(--p3-bg)}
.card.p2{border-left-color:var(--p2);background:var(--p2-bg)}
.card.active{box-shadow:0 0 0 1px currentColor}
.card.p5.active{box-shadow:0 0 0 1px var(--p5)}
.card.p4.active{box-shadow:0 0 0 1px var(--p4)}
.card.p3.active{box-shadow:0 0 0 1px var(--p3)}
.card.p2.active{box-shadow:0 0 0 1px var(--p2)}
.card-header{
  display:flex;align-items:baseline;justify-content:space-between;
  gap:1rem;margin-bottom:.4rem;
}
.event-name{font-size:1rem;font-weight:700}
.card-meta{font-size:.8rem;font-weight:600;white-space:nowrap}
.badge{
  display:inline-block;font-size:.6rem;font-weight:700;
  padding:.1rem .35rem;border-radius:.25rem;margin-right:.4rem;
  vertical-align:middle;
}
.p5 .badge{background:var(--p5);color:#fff}
.p4 .badge{background:var(--p4);color:#fff}
.p3 .badge{background:var(--p3);color:#fff}
.p2 .badge{background:var(--p2);color:#fff}
.active-badge{
  display:inline-block;font-size:.6rem;font-weight:700;
  padding:.1rem .35rem;border-radius:.25rem;margin-left:.4rem;
  vertical-align:middle;animation:pulse 2s infinite;
}
.p5 .active-badge{background:var(--p5);color:#fff}
.p4 .active-badge{background:var(--p4);color:#fff}
.p3 .active-badge{background:var(--p3);color:#fff}
.p2 .active-badge{background:var(--p2);color:#fff}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.expires{font-size:.65rem;color:var(--muted);margin-bottom:.5rem}
.eee{font-size:.65rem;color:var(--muted);margin-bottom:.5rem;font-family:monospace}
.header-msg{
  font-size:.75rem;color:var(--muted);margin-bottom:.75rem;
  word-break:break-all;font-family:monospace;
}
.transcript-wrap{
  margin-top:.75rem;padding-top:.75rem;
  border-top:1px solid rgba(255,255,255,.07);
}
.transcript-label{
  font-size:.6rem;font-weight:700;letter-spacing:.1em;
  text-transform:uppercase;color:var(--muted);margin-bottom:.35rem;
}
.transcript{font-size:.9rem;line-height:1.6}
.transcript-pending,.transcript-failed{font-size:.8rem;color:var(--muted);font-style:italic}
.src-badge{
  display:inline-block;font-size:.55rem;font-weight:700;letter-spacing:.08em;
  padding:.1rem .35rem;border-radius:.2rem;margin-left:.35rem;
  vertical-align:middle;border:1px solid var(--border);color:var(--muted);
}
.src-radio{border-color:#3b82f6;color:#60a5fa}
.src-nwws{border-color:#a855f7;color:#c084fc}
.src-api{border-color:#10b981;color:#34d399}
.src-test{border-color:#f59e0b;color:#fbbf24}
.card.test{border-style:dashed;opacity:.85}
.headline{font-size:.85rem;font-weight:600;margin:.5rem 0;line-height:1.4}
.alert-details{margin-top:.5rem}
.alert-details summary{
  font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);cursor:pointer;user-select:none;
}
.alert-text{
  white-space:pre-wrap;font-family:inherit;font-size:.8rem;line-height:1.55;
  color:var(--text);margin:.5rem 0 0;max-height:24rem;overflow-y:auto;
}
.alert-map{
  height:320px;margin-top:.5rem;border-radius:.4rem;
  border:1px solid var(--border);background:var(--bg);
}
.leaflet-container{background:var(--bg)}
audio{width:100%;margin-top:.6rem;height:2rem}
.empty{
  text-align:center;color:var(--muted);padding:2.5rem;
  font-size:.85rem;border:1px dashed var(--border);border-radius:.5rem;
}
section{margin-bottom:2rem}
.pagination{
  display:flex;align-items:center;justify-content:center;
  gap:.75rem;margin-top:.75rem;
}
.page-btn{
  background:var(--surface);border:1px solid var(--border);color:var(--text);
  padding:.3rem .75rem;border-radius:.25rem;font-size:.75rem;
  cursor:pointer;font-family:inherit;
}
.page-btn:hover:not(:disabled){background:var(--border)}
.page-btn:disabled{opacity:.3;cursor:default}
.page-info{font-size:.7rem;color:var(--muted)}
.live-player{
  background:var(--surface);border:1px solid var(--border);border-radius:.5rem;
  padding:.75rem 1rem;margin-bottom:1.5rem;
  display:flex;align-items:center;gap:1rem;
}
.live-dot{
  width:.5rem;height:.5rem;border-radius:50%;
  background:var(--muted);flex-shrink:0;
}
.live-dot.connected{background:#22c55e;animation:pulse 2s infinite}
.live-label{font-size:.65rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);flex-shrink:0}
.live-player audio{flex:1;height:2rem;min-width:0}
.notif-bar{
  background:var(--surface);border:1px solid var(--border);border-radius:.5rem;
  padding:.75rem 1rem;margin-bottom:.75rem;
  display:flex;align-items:center;gap:.75rem;
}
.notif-dot{width:.5rem;height:.5rem;border-radius:50%;background:var(--muted);flex-shrink:0}
.notif-dot.active{background:#22c55e;animation:pulse 2s infinite}
.notif-dot.denied{background:var(--p5)}
.notif-sel{
  background:var(--bg);border:1px solid var(--border);color:var(--text);
  padding:.2rem .5rem;border-radius:.25rem;font-size:.7rem;font-family:inherit;cursor:pointer;
}
.notif-btn{
  background:var(--surface);border:1px solid var(--border);color:var(--text);
  padding:.3rem .75rem;border-radius:.25rem;font-size:.7rem;
  cursor:pointer;font-family:inherit;margin-left:auto;
}
.notif-btn:hover:not(:disabled){background:var(--border)}
.notif-btn:disabled{opacity:.4;cursor:default}
.notif-toggle{
  background:none;border:1px solid var(--border);color:var(--muted);
  width:1.5rem;height:1.5rem;border-radius:.25rem;
  font-size:.65rem;cursor:pointer;padding:0;flex-shrink:0;
}
.notif-toggle:hover{background:var(--border);color:var(--text)}
.notif-panel{
  background:var(--surface);border:1px solid var(--border);border-radius:.5rem;
  padding:1rem 1.25rem;margin-bottom:.75rem;
}
.notif-groups{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:1rem 1.5rem;
}
.notif-group-label{
  font-size:.6rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);margin-bottom:.4rem;
}
.notif-code{
  display:flex;align-items:baseline;gap:.35rem;
  font-size:.72rem;color:var(--text);margin-bottom:.18rem;cursor:pointer;
  line-height:1.3;
}
.notif-code input{cursor:pointer;flex-shrink:0;margin-top:.1rem}
.notif-code span{font-family:monospace;color:var(--muted);font-size:.65rem;flex-shrink:0}
.site-footer{
  margin-top:2rem;padding-top:1rem;border-top:1px solid var(--border);
  text-align:center;font-size:.7rem;color:var(--muted);
}
</style>
</head>
<body>
<header>
  <div class="header-brand">
    <img src="/icons/logo.png" alt="NOAA Weather Radio All Hazards" class="nwr-logo">
    <div>
      <div class="header-title">__TITLE__</div>
      <div class="header-subtitle">__SUBTITLE__</div>
    </div>
  </div>
  <div class="header-right">
    <div class="status-chips" id="src-health"></div>
    <button class="theme-toggle" id="theme-toggle" type="button"
            title="Theme: auto / light / dark" onclick="cycleTheme()">◐</button>
  </div>
</header>

__LIVE_PLAYER__

<div class="notif-bar" id="notif-bar" style="display:none">
  <div class="notif-dot" id="notif-dot"></div>
  <span class="live-label">Notifications</span>
  <select id="notif-priority" class="notif-sel" style="display:none" onchange="updatePriority()">
    <option value="2">All alerts</option>
    <option value="3" selected>Moderate and above</option>
    <option value="4">High and above</option>
    <option value="5">Critical only</option>
    <option value="custom">Custom…</option>
  </select>
  <button id="notif-toggle" class="notif-toggle" style="display:none" onclick="toggleCustomPanel()">▾</button>
  <button id="notif-btn" class="notif-btn">Enable</button>
</div>
<div id="notif-panel" class="notif-panel" style="display:none">
  <div class="notif-groups" id="notif-groups"></div>
</div>

<section id="active-section">
  <div class="section-label" style="display:flex;align-items:center;justify-content:space-between;gap:1rem">
    <span>Active Alerts</span>
    <span class="updated" id="status">loading…</span>
  </div>
  <div id="active"></div>
</section>

<section>
  <div class="section-label" style="display:flex;align-items:center;justify-content:space-between;gap:1rem">
    <span>Alert History</span>
    <div class="history-controls">
      <label class="ctrl-toggle">
        <input type="checkbox" id="hide-rwt" onchange="onHideRwtChange(this)"> Hide test alerts
      </label>
      <button class="page-btn test-btn" id="test-btn" onclick="sendTestAlert(this)" title="Inject a demo alert through the full pipeline">Send test alert</button>
    </div>
  </div>
  <div id="history"></div>
</section>

<script>
const esc = s => String(s ?? '')
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

const PC = p => p >= 5 ? 'p5' : p === 4 ? 'p4' : p === 3 ? 'p3' : 'p2';
const PL = p => p >= 5 ? 'CRITICAL' : p === 4 ? 'HIGH' : p === 3 ? 'MODERATE' : 'LOW';

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleString();
}

function fmtTimeRange(alertTime, expiresAt) {
  const fmtDate = ts => new Date(ts * 1000).toLocaleDateString([], {month:'short', day:'numeric'});
  const fmtT    = ts => new Date(ts * 1000).toLocaleTimeString([], {hour:'numeric', minute:'2-digit'});
  const a = new Date(alertTime * 1000);
  const issued = `${fmtDate(alertTime)}, ${fmtT(alertTime)}`;
  if (!expiresAt) return issued;
  const e = new Date(expiresAt * 1000);
  const end = a.toDateString() === e.toDateString()
    ? fmtT(expiresAt)
    : `${fmtDate(expiresAt)}, ${fmtT(expiresAt)}`;
  return `${issued} – ${end}`;
}

function fmtCountdown(expiresAt) {
  const secs = Math.round(expiresAt - Date.now() / 1000);
  if (secs <= 0) return 'expiring…';
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  const parts = [];
  if (d > 0) parts.push(`${d}d`);
  if (h > 0) parts.push(`${h}h`);
  if (m > 0) parts.push(`${m}m`);
  if (s > 0 || parts.length === 0) parts.push(`${s}s`);
  return `expires in ${parts.join(' ')}`;
}

function audioHtml(a) {
  if (!a.audio_file) return '';
  return `<audio controls src="/audio/${esc(a.id)}.wav"></audio>`;
}

function srcBadges(a) {
  let srcs = [];
  try { srcs = Object.keys(JSON.parse(a.sources || '{}')); } catch(_) {}
  if (a.is_test) srcs.push('test');
  const lbl = {radio: 'RADIO', nwws: 'NWWS', api: 'API', test: 'TEST'};
  return srcs.map(s =>
    `<span class="src-badge src-${esc(s)}">${lbl[s] || esc(s).toUpperCase()}</span>`
  ).join('');
}

function detailsHtml(a) {
  const head = a.headline ? `<div class="headline">${esc(a.headline)}</div>` : '';
  let body = '';
  if (a.description) {
    const instr = a.instruction
      ? '\n\nPRECAUTIONARY/PREPAREDNESS ACTIONS:\n\n' + a.instruction : '';
    body = `<details class="alert-details"><summary>Full alert text</summary>` +
           `<pre class="alert-text">${esc(a.description + instr)}</pre></details>`;
  }
  return head + body;
}

function voiceHtml(a) {
  // Legacy rows (pre-rearchitecture) may carry a whisper transcript
  const legacy = a.transcript ? `<div class="transcript">${esc(a.transcript)}</div>` : '';
  if (!a.audio_file && !legacy) return '';
  return `<div class="transcript-wrap">
    <div class="transcript-label">Broadcast Recording</div>
    ${legacy}${audioHtml(a)}
  </div>`;
}

// ── Per-alert maps (Leaflet over the local tile cache) ───────────────────────
const PRIORITY_HEX = {5: '#dc2626', 4: '#ea580c', 3: '#ca8a04', 2: '#2563eb', 1: '#64748b'};
let _maps = {};       // alert id → L.map
let _mapMeta = {zoom_min: 6, zoom_max: 11, ready: true};

function mapHtml(a, active) {
  if (typeof L === 'undefined') return '';
  if (!a.geometry && !a.fips && !a.ugc) return '';
  // Active alerts show their map expanded by default; history stays collapsed.
  return `<details class="alert-details" data-mapdetails="${esc(a.id)}"${active ? ' open' : ''}
            ontoggle="if(this.open) initAlertMap('${esc(a.id)}')">
    <summary>Map</summary><div class="alert-map" id="map-${esc(a.id)}"></div>
  </details>`;
}

function initOpenMaps() {
  // <details open> rendered from a string never fires ontoggle, so initialize
  // any already-open map panels (default-open active alerts) explicitly.
  document.querySelectorAll('details[data-mapdetails][open]')
    .forEach(d => initAlertMap(d.dataset.mapdetails));
}

async function initAlertMap(id) {
  if (_maps[id]) { _maps[id].invalidateSize(); return; }
  const a = allAlerts.find(x => x.id === id);
  const el = document.getElementById('map-' + id);
  if (!a || !el) return;

  let geojson = null;
  if (a.geometry) {
    try { geojson = JSON.parse(a.geometry); } catch(_) {}
  }
  if (!geojson) {
    const keys = [];
    try { keys.push(...JSON.parse(a.fips || '[]')); } catch(_) {}
    try { keys.push(...JSON.parse(a.ugc || '[]').filter(u => u[2] === 'Z')); } catch(_) {}
    if (keys.length) {
      try {
        const r = await fetch('/api/zonegeo?fips=' + encodeURIComponent(keys.join(',')));
        if (r.ok) {
          const fc = await r.json();
          if (fc.features.length) geojson = fc;
        }
      } catch(_) {}
    }
  }
  if (!geojson) { el.innerHTML = '<div class="empty">No map data cached.</div>'; return; }

  const map = L.map(el, {attributionControl: false, zoomSnap: 0.5});
  L.tileLayer('/tiles/{z}/{x}/{y}.png', {
    minZoom: Math.max(3, _mapMeta.zoom_min - 1),
    maxZoom: _mapMeta.zoom_max + 2,
    minNativeZoom: _mapMeta.zoom_min,
    maxNativeZoom: _mapMeta.zoom_max,
  }).addTo(map);
  L.control.attribution({prefix: false})
    .addAttribution('&copy; OpenStreetMap contributors').addTo(map);
  const color = PRIORITY_HEX[a.priority] || PRIORITY_HEX[3];
  const layer = L.geoJSON(geojson, {
    style: {color: color, weight: 3, fillColor: color, fillOpacity: 0.25},
  }).addTo(map);
  map.fitBounds(layer.getBounds().pad(0.25));
  _maps[id] = map;
}

function saveOpenMaps() {
  return Array.from(document.querySelectorAll('[data-mapdetails]'))
    .filter(d => d.open).map(d => d.dataset.mapdetails);
}

function destroyMaps() {
  Object.values(_maps).forEach(m => { try { m.remove(); } catch(_) {} });
  _maps = {};
}

function restoreOpenMaps(ids) {
  ids.forEach(id => {
    const d = document.querySelector(`[data-mapdetails="${CSS.escape(id)}"]`);
    if (d) d.open = true;   // ontoggle fires initAlertMap
  });
}

function card(a, active) {
  const pc = PC(a.priority);
  const activeBadge = active
    ? `<span class="active-badge">ACTIVE</span>` : '';
  const countdown = active && a.expires_at
    ? `<div class="expires" data-expires="${a.expires_at}">${fmtCountdown(a.expires_at)}</div>`
    : (a.expires_at ? `<div class="expires">Expired ${fmtTime(a.expires_at)}</div>` : '');
  return `
  <div class="card ${pc}${active ? ' active' : ''}${a.is_test ? ' test' : ''}">
    <div class="card-header">
      <span class="event-name">
        <span class="badge">${PL(a.priority)}</span>${esc(a.event_name)}${activeBadge}${srcBadges(a)}
      </span>
      <span class="card-meta">${fmtTimeRange(a.alert_time, a.expires_at)}</span>
    </div>
    ${countdown}
    <div class="eee">EEE: ${esc(a.eee)}</div>
    <div class="header-msg">${esc(a.header_message)}</div>
    ${detailsHtml(a)}
    ${mapHtml(a, active)}
    ${voiceHtml(a)}
  </div>`;
}

function tickCountdowns() {
  document.querySelectorAll('[data-expires]').forEach(el => {
    el.textContent = fmtCountdown(parseFloat(el.dataset.expires));
  });
}

let allAlerts = [];
let hideTests = localStorage.getItem('hideTests') !== 'false';
let historyPage = 0;
const PAGE_SIZE = 3;

// Test-category alerts: the demo "Send test alert" rows plus the weekly/monthly
// test EAS products. "Hide test alerts" hides all of these and the send button.
const TEST_EEE = new Set(['RWT', 'RMT', 'NPT', 'NST', 'NAT', 'DMO']);
const isTestAlert = a => !!a.is_test || TEST_EEE.has(a.eee);

function applyTestUI() {
  const btn = document.getElementById('test-btn');
  if (btn) btn.style.display = hideTests ? 'none' : '';
}

function saveAudioStates() {
  const states = {};
  document.querySelectorAll('.card audio').forEach(el => {
    if (el.currentTime > 0) states[el.src] = {t: el.currentTime, paused: el.paused};
  });
  return states;
}

function restoreAudioStates(states) {
  if (!Object.keys(states).length) return;
  document.querySelectorAll('.card audio').forEach(el => {
    const s = states[el.src];
    if (!s) return;
    el.addEventListener('loadedmetadata', () => {
      el.currentTime = s.t;
      if (!s.paused) el.play().catch(() => {});
    }, {once: true});
    el.load();
  });
}

function renderHistory(history) {
  const total = Math.max(1, Math.ceil(history.length / PAGE_SIZE));
  historyPage = Math.min(historyPage, total - 1);
  const slice = history.slice(historyPage * PAGE_SIZE, (historyPage + 1) * PAGE_SIZE);

  let html = slice.length
    ? slice.map(a => card(a, false)).join('')
    : '<div class="empty">No historical alerts yet.</div>';

  if (total > 1) {
    html += `<div class="pagination">
      <button class="page-btn" onclick="changePage(-1)" ${historyPage === 0 ? 'disabled' : ''}>&#8592; Prev</button>
      <span class="page-info">Page ${historyPage + 1} of ${total}</span>
      <button class="page-btn" onclick="changePage(1)" ${historyPage >= total - 1 ? 'disabled' : ''}>Next &#8594;</button>
    </div>`;
  }

  document.getElementById('history').innerHTML = html;
}

function changePage(delta) {
  historyPage += delta;
  const now = Date.now() / 1000;
  const vis = hideTests ? allAlerts.filter(a => !isTestAlert(a)) : allAlerts;
  const history = vis.filter(a => !a.expires_at || a.expires_at <= now);
  const saved = saveAudioStates();
  renderHistory(history);
  restoreAudioStates(saved);
}

function onHideRwtChange(cb) {
  hideTests = cb.checked;
  localStorage.setItem('hideTests', hideTests);
  render(allAlerts);
}

async function sendTestAlert(btn) {
  if (!confirm('Send a test alert through the full pipeline? ' +
               'This fires real notifications to all configured targets.')) return;
  btn.disabled = true;
  btn.textContent = 'Sending…';
  try {
    const r = await fetch('/api/test-alert', {method: 'POST'});
    btn.textContent = r.ok ? 'Test sent ✓' : 'Failed';
  } catch(_) {
    btn.textContent = 'Failed';
  }
  setTimeout(() => { btn.textContent = 'Send test alert'; btn.disabled = false; }, 4000);
}

function render(alerts) {
  allAlerts = alerts;
  applyTestUI();
  const now = Date.now() / 1000;
  const vis = hideTests ? alerts.filter(a => !isTestAlert(a)) : alerts;
  const active  = vis.filter(a => a.expires_at && a.expires_at > now);
  const history = vis.filter(a => !a.expires_at || a.expires_at <= now);

  const saved = saveAudioStates();
  const openMaps = saveOpenMaps();
  destroyMaps();

  document.getElementById('active').innerHTML = active.length
    ? active.map(a => card(a, true)).join('')
    : '<div class="empty">No active alerts.</div>';

  renderHistory(history);
  restoreAudioStates(saved);
  restoreOpenMaps(openMaps);
  initOpenMaps();   // active-alert maps render expanded by default
}

// Tick countdowns every second without hitting the server
setInterval(tickCountdowns, 1000);

// Status (frequency) — slow poll, changes rarely
async function pollStatus() {
  try {
    const r = await fetch('/api/status');
    if (r.ok) {
      const st = await r.json();
      renderSourceHealth(st);
      if (st.map && st.map.zoom_max) _mapMeta = st.map;
    }
  } catch(_) {}
  setTimeout(pollStatus, 30000);
}

function healthChip(cls, label, ok) {
  // ● = working, ○ (dimmed) = enabled but not currently working
  return `<span class="src-badge ${cls}" style="${ok ? '' : 'opacity:.4'}"` +
         ` title="${ok ? 'online' : 'down'}">${esc(label)} ${ok ? '●' : '○'}</span>`;
}

function renderSourceHealth(st) {
  const radio = st.radio || {};
  const nwws  = st.nwws  || {};
  const api   = st.api   || {};
  const now = Date.now() / 1000;
  const chips = [];
  // Every *enabled* source gets a chip and shows its up/down state. The radio
  // chip carries the tuned frequency while audio is flowing, else "RADIO".
  if (radio.enabled) {
    const ok = !!radio.alive;
    const freq = st.frequency && st.frequency !== '—' ? `${st.frequency} MHz` : 'RADIO';
    chips.push(healthChip('src-radio', ok ? freq : 'RADIO', ok));
  }
  if (nwws.enabled) {
    chips.push(healthChip('src-nwws', 'NWWS', !!nwws.connected));
  }
  if (api.enabled) {
    const ok = !!(api.last_success_ts && (now - api.last_success_ts) < 600);
    chips.push(healthChip('src-api', 'API', ok));
  }
  document.getElementById('src-health').innerHTML = chips.join('');
}

// SSE — server pushes alert snapshots whenever the DB changes, so a loaded
// page updates live with no refresh. The label shows the last push time.
function setUpdated(text) {
  document.getElementById('status').textContent = text;
}
function connectSSE() {
  const es = new EventSource('/events');
  es.onopen = () => setUpdated('live');
  es.onmessage = (e) => {
    render(JSON.parse(e.data));
    setUpdated(`updated ${new Date().toLocaleTimeString()}`);
  };
  es.addEventListener('ping', () => {
    // keep-alive proves the stream is live even when nothing has changed
    const cur = document.getElementById('status').textContent;
    if (cur === 'live' || cur === 'loading…') setUpdated('live');
  });
  es.onerror = () => setUpdated('reconnecting…');
}

// ── Web Push ──────────────────────────────────────────────────────────────────
const EAS_GROUPS = [
  ['Tornado',[['TOR','Tornado Warning'],['TOA','Tornado Watch'],['TOE','Tornado Emergency']]],
  ['Severe Thunderstorm',[['SVR','Severe Thunderstorm Warning'],['SVA','Severe Thunderstorm Watch'],['SVS','Severe Weather Statement'],['EWW','Extreme Wind Warning'],['SPS','Special Weather Statement'],['SQW','Snow Squall Warning']]],
  ['Flood',[['FFW','Flash Flood Warning'],['FFA','Flash Flood Watch'],['FFS','Flash Flood Statement'],['FLW','Flood Warning'],['FLA','Flood Watch'],['FLS','Flood Statement'],['CFW','Coastal Flood Warning'],['CFA','Coastal Flood Watch'],['DBA','Dam Break Watch'],['DBW','Dam Break Warning']]],
  ['Tropical & Coastal',[['HUW','Hurricane Warning'],['HUA','Hurricane Watch'],['HLS','Hurricane Local Statement'],['TRW','Tropical Storm Warning'],['TRA','Tropical Storm Watch'],['TSW','Tsunami Warning'],['TSA','Tsunami Watch'],['SSW','Storm Surge Warning'],['SSA','Storm Surge Watch']]],
  ['Winter & Wind',[['WSW','Winter Storm Warning'],['WSA','Winter Storm Watch'],['WFW','Winter Fire Weather Warning'],['WFA','Winter Fire Weather Watch'],['BZW','Blizzard Warning'],['HWW','High Wind Warning'],['HWA','High Wind Watch'],['FZW','Freeze Warning'],['FSW','Freezing Spray Warning'],['BHW','Blowing Dust Warning'],['BWW','Brisk Wind Warning'],['DSW','Dust Storm Warning']]],
  ['Civil Emergency',[['EAN','Emergency Action Notification'],['EAT','Emergency Action Termination'],['NIC','National Information Center'],['LAE','Local Area Emergency'],['CEM','Civil Emergency Message'],['CDW','Civil Danger Warning'],['CAE','Child Abduction Emergency'],['EVI','Evacuation – Immediate'],['EVA','Evacuation Watch'],['LEW','Law Enforcement Warning'],['SPW','Shelter-in-Place Warning'],['NUW','Nuclear Power Plant Warning'],['RHW','Radiological Hazard Warning']]],
  ['Other Hazards',[['EQW','Earthquake Warning'],['VOW','Volcano Warning'],['LSW','Landslide Warning'],['HMW','Hazardous Materials Warning'],['FRW','Fire Warning'],['IFW','Industrial Fire Warning'],['CWW','Contaminated Water Warning'],['CHW','Chemical Hazard Warning'],['IBW','Iceberg Warning'],['POS','Power Outage Statement'],['SMW','Special Marine Warning'],['ADR','Administrative Message']]],
  ['Advisories (non-EAS)',[['EHW','Excessive Heat Warning'],['EHA','Excessive Heat Watch'],['HTY','Heat Advisory'],['ECW','Extreme Cold Warning'],['ECA','Extreme Cold Watch'],['WCW','Wind Chill Warning'],['WCY','Wind Chill Advisory'],['WWY','Winter Weather Advisory'],['WIY','Wind Advisory'],['FGY','Dense Fog Advisory'],['FRY','Frost Advisory'],['FZA','Freeze Watch'],['HZW','Hard Freeze Warning'],['HZA','Hard Freeze Watch'],['RFW','Red Flag Warning'],['FWA','Fire Weather Watch'],['DUY','Blowing Dust Advisory'],['AQA','Air Quality Alert']]],
  ['Tests',[['RWT','Required Weekly Test'],['RMT','Required Monthly Test'],['NPT','National Periodic Test'],['NST','National Silent Test'],['NAT','National Audible Test'],['DMO','Practice Demo']]],
];

let _swReg = null;

function _b64ToUint8(b64) {
  const pad = '='.repeat((4 - b64.length % 4) % 4);
  const raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

function _buildEventGrid(checkedCodes) {
  document.getElementById('notif-groups').innerHTML = EAS_GROUPS.map(([group, codes]) =>
    `<div><div class="notif-group-label">${group}</div>` +
    codes.map(([code, name]) =>
      `<label class="notif-code"><input type="checkbox" value="${code}"` +
      (checkedCodes && checkedCodes.includes(code) ? ' checked' : '') +
      ` onchange="_saveCustomCodes()"><span>${code}</span> ${name}</label>`
    ).join('') + '</div>'
  ).join('');
}

async function _saveCustomCodes() {
  const sub = await _swReg.pushManager.getSubscription();
  if (!sub) return;
  const codes = Array.from(document.querySelectorAll('#notif-groups input:checked')).map(el => el.value);
  await fetch('/push/subscribe', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({subscription: sub.toJSON(), eventCodes: codes}),
  });
}

function toggleCustomPanel() {
  const panel  = document.getElementById('notif-panel');
  const toggle = document.getElementById('notif-toggle');
  const opening = panel.style.display === 'none';
  panel.style.display  = opening ? '' : 'none';
  toggle.textContent   = opening ? '▴' : '▾';
}

async function _restorePrefs(sub) {
  try {
    const r = await fetch('/push/info?endpoint=' + encodeURIComponent(sub.endpoint));
    if (!r.ok) return;
    const {minPriority, eventCodes} = await r.json();
    const sel    = document.getElementById('notif-priority');
    const panel  = document.getElementById('notif-panel');
    const toggle = document.getElementById('notif-toggle');
    if (eventCodes !== null) {
      sel.value = 'custom';
      _buildEventGrid(eventCodes);
      panel.style.display  = 'none';   // start collapsed — codes already saved
      toggle.style.display = '';
      toggle.textContent   = '▾';
    } else {
      sel.value            = String(minPriority || 3);
      panel.style.display  = 'none';
      toggle.style.display = 'none';
    }
  } catch(_) {}
}

const PUSH_ENABLED = __PUSH_ENABLED__;

async function initPush() {
  if (!PUSH_ENABLED) return;   // disabled via WEB_PUSH_ENABLED
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
  try {
    _swReg = await navigator.serviceWorker.ready;
    document.getElementById('notif-bar').style.display = '';
    await _refreshPushUI();
  } catch(_) {}
}

async function _refreshPushUI() {
  const sub   = await _swReg.pushManager.getSubscription();
  const perm  = Notification.permission;
  const dot   = document.getElementById('notif-dot');
  const btn   = document.getElementById('notif-btn');
  const sel   = document.getElementById('notif-priority');
  const panel = document.getElementById('notif-panel');
  dot.className = 'notif-dot';
  if (perm === 'denied') {
    dot.classList.add('denied');
    btn.textContent = 'Blocked in browser';
    btn.disabled = true;
    sel.style.display = 'none';
    panel.style.display = 'none';
  } else if (sub) {
    dot.classList.add('active');
    btn.textContent = 'Disable';
    btn.disabled = false;
    btn.onclick = _disablePush;
    sel.style.display = '';
    await _restorePrefs(sub);
  } else {
    btn.textContent = 'Enable';
    btn.disabled = false;
    btn.onclick = _enablePush;
    sel.style.display = 'none';
    panel.style.display = 'none';
  }
}

async function _enablePush() {
  const perm = await Notification.requestPermission();
  if (perm !== 'granted') { await _refreshPushUI(); return; }
  const {key} = await fetch('/push/vapid-public-key').then(r => r.json());
  const sub = await _swReg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: _b64ToUint8(key),
  });
  await fetch('/push/subscribe', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({subscription: sub.toJSON(), minPriority: 3}),
  });
  await _refreshPushUI();
}

async function _disablePush() {
  const sub = await _swReg.pushManager.getSubscription();
  if (sub) {
    await fetch('/push/unsubscribe', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({endpoint: sub.endpoint}),
    });
    await sub.unsubscribe();
  }
  document.getElementById('notif-panel').style.display = 'none';
  await _refreshPushUI();
}

async function updatePriority() {
  const val    = document.getElementById('notif-priority').value;
  const panel  = document.getElementById('notif-panel');
  const toggle = document.getElementById('notif-toggle');
  if (val === 'custom') {
    if (!document.getElementById('notif-groups').children.length) _buildEventGrid(null);
    panel.style.display  = '';
    toggle.style.display = '';
    toggle.textContent   = '▴';
    return;  // each checkbox saves itself via onchange
  }
  panel.style.display  = 'none';
  toggle.style.display = 'none';
  const sub = await _swReg.pushManager.getSubscription();
  if (!sub) return;
  await fetch('/push/subscribe', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({subscription: sub.toJSON(), minPriority: parseInt(val), eventCodes: null}),
  });
}

// ── Light / dark theme (auto = follow OS, then light, then dark) ─────────────
const THEME_ORDER = ['auto', 'light', 'dark'];
const THEME_ICON  = {auto: '◐', light: '☀', dark: '☾'};

function currentTheme() {
  return localStorage.getItem('theme') || 'auto';
}
function applyTheme(t) {
  if (t === 'auto') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('theme', t);
  refreshThemeBtn();
}
function refreshThemeBtn() {
  const t   = currentTheme();
  const btn = document.getElementById('theme-toggle');
  if (!btn) return;
  btn.textContent = THEME_ICON[t];
  btn.title = `Theme: ${t} (click to change)`;
}
function cycleTheme() {
  const next = THEME_ORDER[(THEME_ORDER.indexOf(currentTheme()) + 1) % THEME_ORDER.length];
  applyTheme(next);
}
refreshThemeBtn();

document.getElementById('hide-rwt').checked = hideTests;
applyTestUI();
pollStatus();
connectSSE();
initPush();

// Live radio — green dot while playing, auto-reconnect on stream end
(function() {
  const audio = document.getElementById('live-audio');
  const dot   = document.getElementById('live-dot');
  if (!audio) return;   // Live Radio player omitted (RADIO_ENABLED=false)

  audio.addEventListener('playing', () => dot.classList.add('connected'));
  audio.addEventListener('pause',   () => dot.classList.remove('connected'));
  audio.addEventListener('ended',   reconnect);
  audio.addEventListener('error',   () => { dot.classList.remove('connected'); setTimeout(reconnect, 5000); });

  function reconnect() {
    if (audio.paused) return;           // user paused — don't restart
    dot.classList.remove('connected');
    audio.src = '/stream?' + Date.now(); // cache-bust so browser re-fetches
    audio.play().catch(() => {});
  }
})();
</script>
<script>
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js');
}
</script>
<footer class="site-footer">__FOOTER__</footer>
</body>
</html>
"""


@app.route('/')
def index():
    page = (_HTML
        .replace('__TITLE__',    _html.escape(SITE_TITLE))
        .replace('__SUBTITLE__', _html.escape(_resolved_subtitle()))
        .replace('__FOOTER__',   _html.escape(SITE_FOOTER))
        .replace('__PUSH_ENABLED__', 'true' if WEB_PUSH_ENABLED else 'false')
        .replace('__LIVE_PLAYER__', _LIVE_PLAYER_HTML if RADIO_ENABLED else '')
    )
    return page, 200, {'Content-Type': 'text/html; charset=utf-8'}



@app.route('/manifest.json')
def manifest():
    return Response(json.dumps(_MANIFEST), mimetype='application/manifest+json',
                    headers={'Cache-Control': 'no-cache'})


@app.route('/sw.js')
def service_worker():
    return Response(_SW, mimetype='application/javascript',
                    headers={'Service-Worker-Allowed': '/', 'Cache-Control': 'no-cache'})


@app.route('/icons/<filename>')
def icon(filename):
    path = Path('/app/scripts/icons') / filename
    if not path.exists() or path.suffix != '.png':
        abort(404)
    return send_file(path, mimetype='image/png')


@app.route('/push/vapid-public-key')
def push_public_key():
    return jsonify({'key': pushdb.get_public_key()})


@app.route('/push/subscribe', methods=['POST'])
def push_subscribe():
    data  = request.get_json()
    sub   = data['subscription']
    codes = data.get('eventCodes')  # list of EEE strings, or null for priority preset
    pushdb.save_subscription(
        sub['endpoint'],
        sub['keys']['p256dh'],
        sub['keys']['auth'],
        int(data.get('minPriority', 3)),
        event_codes=codes,
    )
    return jsonify({'ok': True})


@app.route('/push/info')
def push_info():
    endpoint = request.args.get('endpoint', '')
    prefs    = pushdb.get_subscription_prefs(endpoint)
    if not prefs:
        abort(404)
    return jsonify(prefs)


@app.route('/push/unsubscribe', methods=['POST'])
def push_unsubscribe():
    data = request.get_json()
    pushdb.delete_subscription(data['endpoint'])
    return jsonify({'ok': True})


def _wav_header(sample_rate=22050, channels=1, bits=16):
    byte_rate   = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    h  = struct.pack('<4sI4s', b'RIFF', 0xFFFFFFFF, b'WAVE')
    h += struct.pack('<4sIHHIIHH', b'fmt ', 16, 1, channels,
                    sample_rate, byte_rate, block_align, bits)
    h += struct.pack('<4sI', b'data', 0xFFFFFFFF)
    return h


@app.route('/stream')
def audio_stream():
    @stream_with_context
    def generate():
        yield _wav_header()
        try:
            with open('/tmp/audio_fifo', 'rb') as f:
                while True:
                    chunk = f.read(4096)
                    if not chunk:
                        break
                    yield chunk
        except OSError:
            pass
    return Response(generate(), mimetype='audio/wav',
                    headers={'Cache-Control': 'no-cache, no-store'})


@app.route('/api/status')
def status():
    try:
        freq = Path('/tmp/current_freq').read_text().strip()
    except OSError:
        freq = None
    import config as cfg
    map_meta = {}
    try:
        with open(Path(cfg.env('MAP_CACHE_DIR', '/alerts/mapdata')) / 'meta.json') as f:
            m = json.load(f)
        map_meta = {'zoom_min': m.get('zoom_min', 6), 'zoom_max': m.get('zoom_max', 11),
                    'ready': bool(m.get('complete'))}
    except (OSError, ValueError):
        pass
    radio_enabled = cfg.env_bool('RADIO_ENABLED', True)
    radio_alive = False
    if radio_enabled:
        try:
            radio_alive = (time.time() - os.path.getmtime('/tmp/radio_alive')) < 30
        except OSError:
            radio_alive = False
    # Enabled state comes from config (source of truth for which chips to show);
    # health comes from the per-source status file. This way an enabled source
    # whose daemon never came up still shows a "down" chip rather than vanishing.
    src = cfg.get_source_status()
    nwws = src.get('nwws', {})
    api  = src.get('api', {})
    return jsonify({
        'frequency': freq,
        'radio': {'enabled': radio_enabled, 'alive': radio_alive},
        'nwws':  {'enabled': cfg.env_bool('NWWS_ENABLED', False),
                  'connected': bool(nwws.get('connected'))},
        'api':   {'enabled': cfg.env_bool('API_ENABLED', True),
                  'last_success_ts': api.get('last_success_ts')},
        'sources': src,
        'map': map_meta,
    })


@app.route('/events')
def events():
    @stream_with_context
    def generate():
        # Send current snapshot immediately so the page loads with data
        yield f'data: {json.dumps(alertdb.get_alerts(200))}\n\n'
        try:
            last_mtime = os.path.getmtime('/tmp/alerts_updated')
        except OSError:
            last_mtime = None
        tick = 0
        while True:
            time.sleep(1)
            tick += 1
            try:
                mtime = os.path.getmtime('/tmp/alerts_updated')
            except OSError:
                mtime = None
            if mtime != last_mtime:
                last_mtime = mtime
                yield f'data: {json.dumps(alertdb.get_alerts(200))}\n\n'
            elif tick % 15 == 0:
                yield 'event: ping\ndata: 1\n\n'  # keep-alive (named so the client can react)
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/alerts')
def list_alerts():
    return jsonify(alertdb.get_alerts(200))


@app.route('/api/alerts/<alert_id>')
def get_alert(alert_id):
    alert = alertdb.get_alert(alert_id)
    if not alert:
        abort(404)
    return jsonify(alert)


@app.route('/audio/<path:filename>')
def serve_audio(filename):
    if not filename.endswith('.wav'):
        abort(400)
    path = Path(alertdb.AUDIO_DIR) / filename
    if not path.exists():
        abort(404)
    return send_file(path, mimetype='audio/wav')


@app.route('/maps/<path:filename>')
def serve_map(filename):
    if not filename.endswith('.png'):
        abort(400)
    path = Path(alertdb.MAPS_DIR) / filename
    if not path.exists():
        abort(404)
    return send_file(path, mimetype='image/png')


@app.route('/tiles/<int:z>/<int:x>/<int:y>.png')
def serve_tile(z, x, y):
    import config as cfg
    path = Path(cfg.env('MAP_CACHE_DIR', '/alerts/mapdata')) / 'tiles' / str(z) / str(x) / f'{y}.png'
    if not path.exists():
        abort(404)
    resp = send_file(path, mimetype='image/png')
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@app.route('/api/zonegeo')
def zone_geo():
    """Cached zone geometries for the given fips/ugc list (county fallback maps)."""
    import config as cfg
    keys = [k for k in (request.args.get('fips', '') + ',' +
                        request.args.get('ugc', '')).replace(' ', ',').split(',') if k]
    zdir = Path(cfg.env('MAP_CACHE_DIR', '/alerts/mapdata')) / 'zones'
    feats = []
    for key in keys[:30]:
        if key[0].isdigit():
            key = cfg.fips_to_county_ugc(cfg.normalize_same(key)) or key
        p = zdir / f'{key}.geojson'
        try:
            with open(p) as f:
                data = json.load(f)
            if data.get('geometry'):
                feats.append({'type': 'Feature', 'id': key,
                              'properties': {'name': data.get('name')},
                              'geometry': data['geometry']})
        except (OSError, ValueError):
            pass
    return jsonify({'type': 'FeatureCollection', 'features': feats})


@app.route('/api/test-alert', methods=['POST'])
def test_alert():
    """Inject a demo alert through the full pipeline (notify, map, MQTT).

    Rows are flagged is_test, styled distinctly, and purged after 24h.
    """
    import time as _t
    import config as cfg
    from ingest import IncomingAlert, ingest
    now = _t.time()
    fips = sorted(cfg.filter_same_codes()) or ['026163']
    alert_id = ingest(IncomingAlert(
        source='test',
        event_name='Test Message',
        issue_ts=now,
        eee='DMO',
        # unique pseudo-VTEC so repeated tests stay separate rows
        vtec={'action': 'NEW', 'office': 'TEST', 'phen': 'XX', 'sig': 'T',
              'etn': '0000', 'key': f'TEST.XX.T.{int(now)}'},
        fips=set(fips),
        expires_ts=now + 900,
        headline='This is a test of the alert pipeline — notifications, '
                 'map rendering, and integrations.',
        description='Test alert generated from the dashboard. It expires in '
                    '15 minutes and is automatically deleted after 24 hours.',
        is_test=True,
    ))
    return jsonify({'id': alert_id})


@app.route('/static/leaflet/<path:filename>')
def serve_leaflet(filename):
    base = Path('/app/scripts/static/leaflet')
    path = (base / filename).resolve()
    if not str(path).startswith(str(base)) or not path.exists():
        abort(404)
    resp = send_file(path)
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


def _cleanup_loop():
    alertdb.cleanup()
    while True:
        time.sleep(86400)
        alertdb.cleanup()


if __name__ == '__main__':
    import config as cfg
    from waitress import serve
    alertdb.init_db()
    pushdb.init_push()
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    # Each SSE alert stream and live-audio stream holds a thread for the life
    # of the connection, so size the pool well above expected concurrent
    # clients. Default suits a handful of dashboard tabs; raise WEB_THREADS if
    # you fan out to more devices.
    threads = cfg.env_int('WEB_THREADS', 24)
    print(f'web: serving on 0.0.0.0:8082 via waitress ({threads} threads)',
          flush=True)
    serve(app, host='0.0.0.0', port=8082, threads=threads,
          channel_timeout=300, ident='nws-alert-dashboard')
