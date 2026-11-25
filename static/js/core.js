// Deminic shared client core.
// - api(): CSRF signed double-submit header + X-Idempotency-Key on every unsafe request
//   (duplicate submissions are rejected / replayed by the server)
// - socket(): Socket.IO authenticated by the session cookie + CSRF token
// - md(): marked + DOMPurify, code highlighted with CodeMirror.runMode, chat#N refs linkified
// No localStorage is used anywhere - UI state lives in Redis via /api/ui-state.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export function uuid() {
  if (window.crypto && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map((x) => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

export function cookie(name) {
  const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
  return m ? decodeURIComponent(m[1]) : '';
}
export const csrfToken = () => cookie('dm_csrf');

export class ApiError extends Error {
  constructor(status, message, data) { super(message); this.status = status; this.data = data || {}; }
}

export async function api(method, url, body, opts = {}) {
  const m = method.toUpperCase();
  const headers = { Accept: 'application/json' };
  const unsafe = !['GET', 'HEAD', 'OPTIONS'].includes(m);
  if (unsafe) {
    headers['X-CSRF-Token'] = csrfToken();
    if (opts.idempotent !== false) headers['X-Idempotency-Key'] = opts.idempotencyKey || uuid();
  }
  let payload;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined && body !== null) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body); }
  let res;
  try {
    res = await fetch(url, { method: m, headers, body: payload, credentials: 'same-origin', signal: opts.signal });
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    throw new ApiError(0, 'Network error - is the server running?');
  }
  if (res.status === 401 && !opts.allow401) {
    const info = await res.clone().json().catch(() => ({}));
    const why = info.code === 'session_replaced' ? '?reason=elsewhere' : '';
    if (!location.pathname.startsWith('/login')) location.href = '/login' + why;
    throw new ApiError(401, info.detail || 'Not signed in', info);
  }
  const ct = res.headers.get('content-type') || '';
  const data = ct.includes('json') ? await res.json().catch(() => ({})) : await res.text();
  if (!res.ok) {
    const msg = (data && (data.detail || data.message)) || `HTTP ${res.status}`;
    throw new ApiError(res.status, typeof msg === 'string' ? msg : JSON.stringify(msg), data);
  }
  return data;
}

// ------------------------------------------------------------------ events
export const bus = new EventTarget();
export const on = (type, fn) => { const h = (e) => fn(e.detail); bus.addEventListener(type, h); return () => bus.removeEventListener(type, h); };
export const emit = (type, detail) => bus.dispatchEvent(new CustomEvent(type, { detail }));

// ------------------------------------------------------------------ socket
let _socket = null;
export function socket() {
  if (_socket) return _socket;
  // eslint-disable-next-line no-undef
  _socket = io({ auth: (cb) => cb({ csrf: csrfToken() }), transports: ['websocket', 'polling'], tryAllTransports: true, reconnectionDelayMax: 8000 });
  for (const ev of ['log', 'job', 'chat:status', 'chat:delta', 'chat:reset', 'chat:done', 'chat:points', 'memory:changed',
    'summary:updated', 'reminder', 'notify', 'task:changed', 'client:changed', 'email:changed', 'news:ready', 'repo:changed',
    'locust:stats', 'scan:result', 'player:play', 'calendar:changed', 'txn:changed', 'reminder:changed']) {
    _socket.on(ev, (data) => emit(ev, data));
  }
  _socket.on('session:replaced', () => { _socket.disconnect(); location.href = '/login?reason=elsewhere'; });
  _socket.on('connect', () => emit('socket:state', { connected: true }));
  _socket.on('disconnect', () => emit('socket:state', { connected: false }));
  _socket.on('connect_error', (e) => emit('socket:state', { connected: false, error: e && e.message }));
  return _socket;
}

export function cancelJob(requestId) {
  return new Promise((resolve) => {
    socket().timeout(8000).emit('job:cancel', { request_id: requestId }, (err, ack) => {
      if (err) resolve({ ok: false, message: 'no response from server' });
      else resolve(ack || { ok: false });
    });
  });
}

// ------------------------------------------------------------------ toasts / modals
export function toast(message, kind = 'info', ms = 3800) {
  const root = $('#toasts');
  if (!root) return;
  const colors = { info: 'text-slate-800 dark:text-slate-100', ok: 'text-emerald-700 dark:text-emerald-300', warn: 'text-amber-700 dark:text-amber-300', error: 'text-rose-700 dark:text-rose-300' };
  const el = document.createElement('div');
  el.className = `toast toast-${kind} pointer-events-auto ${colors[kind] || colors.info}`;
  el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  el.textContent = message;
  root.appendChild(el);
  setTimeout(() => { el.style.transition = 'opacity .3s'; el.style.opacity = '0'; setTimeout(() => el.remove(), 320); }, ms);
}

export function modal({ title = '', body = '', actions = [], wide = false } = {}) {
  return new Promise((resolve) => {
    const root = $('#modal-root') || document.body;
    const back = document.createElement('div');
    back.className = 'modal-backdrop';
    back.innerHTML = `<div class="modal ${wide ? 'max-w-3xl' : ''}" role="dialog" aria-modal="true">
      <h3 class="text-lg font-semibold">${esc(title)}</h3><div class="mt-3 text-sm text-slate-600 dark:text-slate-300 modal-body"></div>
      <div class="mt-6 flex justify-end gap-2 modal-actions"></div></div>`;
    const bodyEl = back.querySelector('.modal-body');
    if (body instanceof Node) bodyEl.appendChild(body); else bodyEl.innerHTML = body;
    const close = (v) => { back.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = (e) => { if (e.key === 'Escape') close(null); };
    document.addEventListener('keydown', onKey);
    back.addEventListener('click', (e) => { if (e.target === back) close(null); });
    const acts = actions.length ? actions : [{ label: 'OK', kind: 'primary', value: true }];
    for (const a of acts) {
      const b = document.createElement('button');
      b.className = a.kind === 'danger' ? 'btn-danger' : a.kind === 'primary' ? 'btn-primary' : 'btn-ghost';
      b.textContent = a.label;
      b.addEventListener('click', () => close(a.value !== undefined ? a.value : a.label));
      back.querySelector('.modal-actions').appendChild(b);
    }
    root.appendChild(back);
    (back.querySelector('input,textarea,select') || back.querySelector('.modal-actions button:last-child'))?.focus();
  });
}

export function confirmModal(title, text, { danger = false, confirmText = 'Confirm' } = {}) {
  return modal({ title, body: `<p>${esc(text)}</p>`, actions: [{ label: 'Cancel', value: false }, { label: confirmText, kind: danger ? 'danger' : 'primary', value: true }] });
}

export function busy(btn, on, label) {
  if (!btn) return;
  if (on) { btn.dataset.label = btn.innerHTML; btn.disabled = true; btn.innerHTML = `<span class="dot-typing"><span></span><span></span><span></span></span>${label ? ' ' + esc(label) : ''}`; }
  else { btn.disabled = false; if (btn.dataset.label) btn.innerHTML = btn.dataset.label; }
}

// ------------------------------------------------------------------ formatting
export function fmtDate(iso, withTime = true) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString(undefined, withTime ? { dateStyle: 'medium', timeStyle: 'short' } : { dateStyle: 'medium' });
}
export function fmtRel(iso) {
  if (!iso) return '';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return fmtDate(iso, false);
}
export function money(v, cur = '') {
  const n = Number(v || 0);
  return (cur ? cur + ' ' : '') + n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

// {business_name} variable - substituted with the CURRENT business name at render time
export const state = { me: null };
export function bizvars(text) {
  const name = state.me?.user?.business_name || 'your business';
  return String(text ?? '').replace(/\{\{\s*business_name\s*\}\}|\{\s*business_name\s*\}/g, name);
}

// ------------------------------------------------------------------ markdown
const REF = /\b(chat|task|client|email|event|reminder|txn)#(\d+)(?:\.(\d+))?\b/gi;
const MODE_ALIASES = { py: 'python', js: 'javascript', ts: 'text/typescript', typescript: 'text/typescript', html: 'htmlmixed', sh: 'shell', bash: 'shell', yml: 'yaml', json: 'application/json', c: 'text/x-csrc', cpp: 'text/x-c++src', java: 'text/x-java', cs: 'text/x-csharp', kotlin: 'text/x-kotlin', md: 'markdown', rs: 'rust', toml: 'toml', sql: 'sql', go: 'go', php: 'php', rb: 'ruby', jsx: 'jsx', tsx: 'text/typescript-jsx', css: 'css', xml: 'xml', diff: 'diff', dockerfile: 'dockerfile', swift: 'swift' };

export function md(text) {
  // eslint-disable-next-line no-undef
  const raw = window.marked ? marked.parse(bizvars(text || ''), { breaks: true, gfm: true }) : esc(text);
  // eslint-disable-next-line no-undef
  return window.DOMPurify ? DOMPurify.sanitize(raw, { FORBID_TAGS: ['style', 'iframe', 'form', 'input', 'object', 'embed'], FORBID_ATTR: ['style', 'onerror', 'onload'] }) : esc(text);
}

const enhancers = [];
/** Section modules can post-process rendered AI markdown: fn(element, rawMarkdown, section). */
export function registerEnhancer(fn) { enhancers.push(fn); }

export function enhance(el, raw = '', section = '') {
  // code highlighting with the same CodeMirror modes as the editor
  for (const code of el.querySelectorAll('pre code')) {
    const cls = [...code.classList].find((c) => c.startsWith('language-')) || '';
    const lang = cls.replace('language-', '').split(/\s/)[0].toLowerCase();
    const mode = MODE_ALIASES[lang] || lang;
    if (window.CodeMirror && CodeMirror.runMode && mode) {
      const txt = code.textContent;
      code.textContent = '';
      try { CodeMirror.runMode(txt, mode, code); code.classList.add('cm-s-default'); } catch { code.textContent = txt; }
    }
    const pre = code.parentElement;
    if (!pre.querySelector('.copy-btn')) {
      pre.classList.add('relative', 'group');
      const b = document.createElement('button');
      b.className = 'copy-btn absolute top-2 right-2 btn-sm btn bg-white/10 text-slate-200 opacity-0 group-hover:opacity-100';
      b.textContent = 'Copy';
      b.addEventListener('click', () => { navigator.clipboard?.writeText(code.textContent); b.textContent = 'Copied'; setTimeout(() => (b.textContent = 'Copy'), 1200); });
      pre.appendChild(b);
    }
  }
  for (const a of el.querySelectorAll('a[href]')) {
    if (!a.getAttribute('href').startsWith('/')) { a.target = '_blank'; a.rel = 'noopener noreferrer'; }
  }
  linkifyRefs(el);
  for (const fn of enhancers) { try { fn(el, raw, section || document.documentElement.dataset.section || ''); } catch (e) { console.error(e); } }
  return el;
}

export function linkifyRefs(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement && n.parentElement.closest('code,pre,a,.ref-link') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  const nodes = [];
  const probe = /\b(chat|task|client|email|event|reminder|txn)#\d+/i;
  while (walker.nextNode()) if (probe.test(walker.currentNode.nodeValue)) nodes.push(walker.currentNode);
  for (const n of nodes) {
    REF.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let last = 0; const s = n.nodeValue; let m;
    while ((m = REF.exec(s))) {
      frag.appendChild(document.createTextNode(s.slice(last, m.index)));
      const a = document.createElement('a');
      a.className = 'ref-link'; a.dataset.ref = m[0]; a.textContent = m[0]; a.href = '#'; a.title = `Open ${m[0]}`;
      frag.appendChild(a);
      last = m.index + m[0].length;
    }
    frag.appendChild(document.createTextNode(s.slice(last)));
    n.replaceWith(frag);
  }
}

export function renderMd(el, text, section = '') { el.innerHTML = md(text); return enhance(el, text, section); }

// ------------------------------------------------------------------ theme (stored server-side in Redis/DB)
export function applyTheme(theme) {
  const t = theme || document.documentElement.dataset.theme || 'system';
  document.documentElement.dataset.theme = t;
  const dark = t === 'dark' || (t === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.classList.toggle('dark', dark);
}
export async function toggleTheme() {
  const dark = document.documentElement.classList.contains('dark');
  const next = dark ? 'light' : 'dark';
  applyTheme(next);
  try { await api('PUT', '/api/theme', { theme: next }, { idempotent: false }); } catch { /* offline */ }
}
export function bindThemeToggles(root = document) {
  for (const b of $$('[data-theme-toggle]', root)) b.addEventListener('click', toggleTheme);
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => applyTheme());
}

// ------------------------------------------------------------------ geolocation (secure context only)
let _loc = null; let _locAt = 0;
export function getLocation() {
  return new Promise((resolve) => {
    if (!window.isSecureContext || !navigator.geolocation) return resolve(null);
    if (_loc && Date.now() - _locAt < 5 * 60 * 1000) return resolve(_loc);
    navigator.geolocation.getCurrentPosition(
      (p) => { _loc = { lat: p.coords.latitude, lon: p.coords.longitude, accuracy: p.coords.accuracy }; _locAt = Date.now(); resolve(_loc); },
      () => resolve(null), { enableHighAccuracy: false, timeout: 6000, maximumAge: 300000 });
  });
}

export function debounce(fn, ms = 250) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
export function el(html) { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; }
