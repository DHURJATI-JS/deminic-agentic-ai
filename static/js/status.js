// System status dialog - opened from the status button next to the theme toggle.
// While open it pings every component every 2 s (real-time latency + a small
// history graph per row), measures the HTTP and live-socket round trips, and
// runs a real "pong" generation through the local AI chain.
import { api, esc, socket } from './core.js';
import { icon } from './icons.js';

const PING_MS = 2000;
const DOT_MS = 60000;
const HIST = 32;
const STATE_LABEL = { ok: 'Operational', info: 'On fallback', warn: 'Degraded', down: 'Down', off: 'Off' };
const HEAD_ICON = { ok: 'check', degraded: 'alert', down: 'x', unknown: 'radio' };

let back = null;
let timer = 0;
let paused = false;
let ticking = false;
let hist = {};
let last = null;
let tests = {};
let lastFocus = null;
let dotTimer = 0;

const fmtMs = (v) => (v == null ? '-' : v < 10 ? `${v.toFixed(1)} ms` : `${Math.round(v)} ms`);
const now = () => performance.now();
const round1 = (v) => Math.round(v * 10) / 10;

function setDots(state) {
  for (const d of document.querySelectorAll('[data-status-dot]')) d.dataset.state = state;
  for (const b of document.querySelectorAll('[data-status-open]')) {
    b.title = `System status - ${state === 'ok' ? 'all systems operational' : state === 'unknown' ? 'checking' : state}`;
  }
}

async function fetchStatus(deep = false) {
  const t0 = now();
  const data = await api('GET', `/api/status/ping${deep ? '?deep=1' : ''}`, undefined, { allow401: true });
  return { data, rtt: round1(now() - t0) };
}

function socketRtt() {
  return new Promise((resolve) => {
    // only where the app already keeps a live connection open (never opens a new one)
    if (typeof window.io !== 'function' || !document.getElementById('app')) { resolve(null); return; }
    const s = socket();
    if (!s.connected) { resolve({ ok: false, ms: null }); return; }
    const t0 = now();
    s.timeout(2500).emit('status:ping', { t: Date.now() }, (err, ack) => {
      resolve(err ? { ok: false, ms: null } : { ok: !!(ack && ack.ok), ms: round1(now() - t0) });
    });
  });
}

function push(key, ms) {
  const h = (hist[key] ||= []);
  h.push(ms);
  if (h.length > HIST) h.shift();
}

function spark(key, state) {
  const h = hist[key] || [];
  if (h.filter((v) => v != null).length < 2) return `<svg class="status-spark" data-state="${state}" viewBox="0 0 64 18" aria-hidden="true"><line x1="0" y1="17" x2="64" y2="17"/></svg>`;
  const max = Math.max(5, ...h.filter((v) => v != null));
  const step = 64 / (HIST - 1);
  const off = HIST - h.length;
  let d = '';
  let pen = false;
  h.forEach((v, i) => {
    if (v == null) { pen = false; return; }
    const x = ((off + i) * step).toFixed(1);
    const y = (16 - (v / max) * 13).toFixed(1);
    d += `${pen ? 'L' : 'M'}${x} ${y}`;
    pen = true;
  });
  const lastV = h[h.length - 1];
  const head = lastV == null ? '' : `<circle cx="64" cy="${(16 - (lastV / max) * 13).toFixed(1)}" r="1.9"/>`;
  return `<svg class="status-spark" data-state="${state}" viewBox="0 0 64 18" aria-hidden="true"><line x1="0" y1="17" x2="64" y2="17"/><path d="${d}"/>${head}</svg>`;
}

function row(r) {
  return `<li class="status-row" data-state="${r.state}">
    <span class="status-led" data-state="${r.state}" aria-hidden="true"></span>
    <div class="min-w-0 flex-1">
      <div class="flex items-center gap-2 min-w-0"><span class="font-medium truncate">${esc(r.label)}</span>
        <span class="status-badge" data-state="${r.state}">${STATE_LABEL[r.state] || r.state}</span></div>
      <div class="text-xs text-slate-500 dark:text-slate-400 line-clamp-2">${esc(r.detail || '')}${r.note ? `${r.detail ? ' · ' : ''}${esc(r.note)}` : ''}</div>
    </div>
    ${spark(r.key, r.state)}
    <span class="status-ms tabular-nums">${r.ms == null ? '' : fmtMs(r.ms)}</span>
  </li>`;
}

function testLine(tier, label) {
  const t = tests[tier];
  let body;
  if (!t) body = '<span class="text-slate-500">Not run yet</span>';
  else if (t === 'running') body = `<span class="status-spinner" aria-hidden="true"></span><span class="text-slate-500">Asking the model to reply "pong"&hellip;</span>`;
  else if (t.error) body = `<span class="text-rose-600 dark:text-rose-400">${esc(t.error)}</span>`;
  else if (t.ok) {
    body = `<span class="${t.correct ? 'text-emerald-600 dark:text-emerald-400' : 'text-amber-600 dark:text-amber-400'} inline-flex items-center gap-1 font-medium">
        ${icon(t.correct ? 'check' : 'alert', 'w-4 h-4')}&ldquo;${esc(t.reply || '')}&rdquo;</span>
      <span class="text-slate-600 dark:text-slate-300">from <b>${esc(t.provider)}</b></span>
      <span class="chip !h-6 tabular-nums" title="Time to first token">first token ${fmtMs(t.ttft_ms)}</span>
      <span class="chip !h-6 tabular-nums" title="Total time">total ${fmtMs(t.total_ms)}</span>
      ${t.fallback_used ? '<span class="chip !h-6 text-amber-700 dark:text-amber-300">failed over</span>' : ''}`;
  } else {
    const tried = (t.attempts || []).map((a) => `${esc(a.provider)}: ${esc(a.error || '')}`).join(' · ');
    const online = (t.online_ready || []).length ? ` Online escalation is still ready: ${esc(t.online_ready.join(', '))}.` : '';
    body = `<span class="text-rose-600 dark:text-rose-400 font-medium">${esc(t.message || 'No answer')}</span><span class="text-xs text-slate-500">${tried}${online}</span>`;
  }
  return `<div class="status-test-row"><span class="status-test-label">${label}</span><div class="flex flex-wrap items-center gap-x-2 gap-y-1 min-w-0">${body}</div></div>`;
}

function renderTests() {
  const box = back?.querySelector('[data-status-tests]');
  if (!box) return;
  const runBtn = back.querySelector('[data-status-run]');
  if (!last) { box.innerHTML = '<p class="text-sm text-slate-500">Checking&hellip;</p>'; runBtn.hidden = true; return; }
  if (!last.signed_in) {
    box.innerHTML = `<p class="text-sm text-slate-500">Sign in to run a live AI response test with your own models.</p>`;
    runBtn.hidden = true;
    return;
  }
  box.innerHTML = testLine('chat', 'Chat AI') + testLine('coding', 'Coding AI');
  runBtn.hidden = false;
  runBtn.disabled = Object.values(tests).includes('running');
}

function render(extra) {
  if (!back || !last) return;
  const state = last.overall;
  const hero = back.querySelector('[data-status-hero]');
  hero.dataset.state = state;
  hero.innerHTML = icon(HEAD_ICON[state] || 'radio', 'w-6 h-6');
  back.querySelector('[data-status-headline]').textContent = last.headline;
  const route = back.querySelector('[data-status-route]');
  route.innerHTML = last.chat_route
    ? `${icon('message', 'w-4 h-4')}<span>Chats are answered by <b>${esc(last.chat_route)}</b></span>`
    : `${icon('alert', 'w-4 h-4')}<span>No model can answer chats right now</span>`;
  const ai = last.components.filter((c) => c.group === 'ai');
  const services = [...extra, ...last.components.filter((c) => c.group === 'services')];
  back.querySelector('[data-status-ai]').innerHTML = ai.map(row).join('');
  back.querySelector('[data-status-services]').innerHTML = services.map(row).join('');
  const up = last.uptime_s;
  const upTxt = up >= 86400 ? `${Math.floor(up / 86400)} d ${Math.floor((up % 86400) / 3600)} h` : up >= 3600 ? `${Math.floor(up / 3600)} h ${Math.floor((up % 3600) / 60)} m` : `${Math.floor(up / 60)} m ${up % 60} s`;
  back.querySelector('[data-status-foot]').textContent = `Last ping ${new Date().toLocaleTimeString()} · server up ${upTxt}`;
  setDots(state);
}

function renderDown(message) {
  if (!back) return;
  const hero = back.querySelector('[data-status-hero]');
  hero.dataset.state = 'down';
  hero.innerHTML = icon('x', 'w-6 h-6');
  back.querySelector('[data-status-headline]').textContent = message;
  back.querySelector('[data-status-route]').innerHTML = `${icon('alert', 'w-4 h-4')}<span>The app server did not answer - retrying every 2 s</span>`;
  back.querySelector('[data-status-foot]').textContent = `Last attempt ${new Date().toLocaleTimeString()}`;
  setDots('down');
}

async function tick(deep = false) {
  if (!back || ticking) return;
  ticking = true;
  try {
    if (!paused && !document.hidden) {
      try {
        const [{ data, rtt }, sock] = await Promise.all([fetchStatus(deep || !last), socketRtt()]);
        const first = !last;
        last = data;
        const extra = [{ key: 'app', group: 'services', label: 'App server', state: 'ok', detail: 'Full status check, round trip from this device', ms: rtt, note: '' }];
        push('app', rtt);
        if (sock) {
          extra.push({ key: 'socket', group: 'services', label: 'Live connection', state: sock.ok ? 'ok' : 'down', detail: 'Socket.IO round trip (streaming replies, cancels)', ms: sock.ms, note: sock.ok ? '' : 'Reconnecting' });
          push('socket', sock.ms);
        }
        for (const c of data.components) if (c.ms != null || c.state === 'down') push(c.key, c.state === 'down' ? null : c.ms);
        render(extra);
        if (first) {
          renderTests();
          if (data.signed_in && !Object.keys(tests).length) runTests();
        }
      } catch (e) {
        if (e.name !== 'AbortError') renderDown(e.status === 0 ? 'App server unreachable' : `Status check failed (${e.message})`);
      }
    }
  } finally {
    ticking = false;
    if (back) timer = setTimeout(() => tick(false), PING_MS);
  }
}

async function runTest(tier) {
  tests[tier] = 'running';
  renderTests();
  try {
    tests[tier] = await api('POST', '/api/status/ai-test', { tier }, { allow401: true });
  } catch (e) {
    tests[tier] = { error: e.status === 409 ? 'A test is already running' : e.status === 429 ? 'Too many tests - wait a minute' : e.message };
  }
  renderTests();
}

function runTests() { runTest('chat'); runTest('coding'); }

function close() {
  if (!back) return;
  clearTimeout(timer);
  timer = 0;
  document.removeEventListener('keydown', onKey);
  back.remove();
  back = null;
  lastFocus?.focus?.();
}

function onKey(e) { if (e.key === 'Escape') close(); }

function setPaused(p) {
  paused = p;
  const btn = back?.querySelector('[data-status-pause]');
  if (btn) {
    btn.innerHTML = icon(p ? 'play' : 'pause', 'w-[18px] h-[18px]');
    btn.title = p ? 'Resume live pinging' : 'Pause live pinging';
    btn.setAttribute('aria-label', btn.title);
  }
  const live = back?.querySelector('[data-status-live]');
  if (live) { live.dataset.paused = p ? '1' : '0'; live.lastElementChild.textContent = p ? 'Paused' : 'Live · every 2 s'; }
}

export function openStatus() {
  if (back) return;
  lastFocus = document.activeElement;
  hist = {};
  tests = {};
  last = null;
  paused = false;
  back = document.createElement('div');
  back.className = 'modal-backdrop';
  back.innerHTML = `<div class="modal status-modal" role="dialog" aria-modal="true" aria-labelledby="status-title">
    <div class="flex items-start gap-3">
      <span class="status-hero" data-status-hero data-state="unknown">${icon('radio', 'w-6 h-6')}</span>
      <div class="min-w-0 flex-1">
        <h3 id="status-title" class="text-lg font-semibold leading-tight">System status</h3>
        <p class="text-sm text-slate-600 dark:text-slate-300 mt-0.5" data-status-headline aria-live="polite">Pinging every component&hellip;</p>
      </div>
      <span class="chip !h-7 hidden sm:inline-flex status-live" data-status-live data-paused="0"><span class="live-dot bg-emerald-500"></span><span>Live · every 2 s</span></span>
      <button class="btn-plain btn-icon" data-status-pause title="Pause live pinging" aria-label="Pause live pinging">${icon('pause', 'w-[18px] h-[18px]')}</button>
      <button class="btn-plain btn-icon" data-status-close title="Close" aria-label="Close">${icon('x', 'w-5 h-5')}</button>
    </div>
    <div class="status-route" data-status-route></div>
    <section class="mt-4">
      <div class="status-section-head"><span>AI response test</span>
        <button class="btn-ghost btn-sm" data-status-run>${icon('zap', 'w-4 h-4')}Run again</button></div>
      <div class="card !p-3 space-y-2" data-status-tests></div>
    </section>
    <section class="mt-4"><div class="status-section-head"><span>AI models</span></div><ul class="status-list" data-status-ai></ul></section>
    <section class="mt-4"><div class="status-section-head"><span>Services</span></div><ul class="status-list" data-status-services></ul></section>
    <div class="mt-4 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
      <span data-status-foot>&nbsp;</span>
      <a href="/technical#failsafe" class="ml-auto">How the AI failsafe works</a>
    </div>
  </div>`;
  (document.getElementById('modal-root') || document.body).appendChild(back);
  back.addEventListener('click', (e) => { if (e.target === back) close(); });
  back.querySelector('[data-status-close]').addEventListener('click', close);
  back.querySelector('[data-status-pause]').addEventListener('click', () => setPaused(!paused));
  back.querySelector('[data-status-run]').addEventListener('click', runTests);
  document.addEventListener('keydown', onKey);
  back.querySelector('[data-status-close]').focus();
  renderTests();
  tick(true);
}

async function refreshDot() {
  if (back || document.hidden) return;
  try {
    const { data } = await fetchStatus(false);
    setDots(data.overall);
  } catch {
    setDots('unknown');
  }
}

export function bindStatusButtons(root = document) {
  for (const b of root.querySelectorAll('[data-status-open]')) {
    if (b.dataset.statusBound) continue;
    b.dataset.statusBound = '1';
    b.addEventListener('click', openStatus);
  }
  if (!dotTimer && root.querySelector('[data-status-dot]')) {
    refreshDot();
    dotTimer = setInterval(refreshDot, DOT_MS);
  }
}
