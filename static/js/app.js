// App shell: sections, tabs (icons + sliding indicator), React-like view
// transitions, chat list, background tasks drawer, terminal, command palette,
// immersive / VR mode, reference navigation.
import { $, $$, api, applyTheme, bindThemeToggles, bizvars, cancelJob, confirmModal, debounce, emit, esc, fmtRel, on, socket, state, toast } from './core.js';
import { registerCard } from './cards.js';
import * as chat from './chat.js';
import * as memory from './memory.js';
import { player } from './player.js';
import { icon, installIconUpgrader } from './icons.js';
import { openPalette } from './palette.js';
import * as terminal from './terminal.js';
import { isImmersive, isQuest, setComfort, setImmersive } from './immersive.js';
import * as navBar from './progress.js';
import { openStatus } from './status.js';
import * as personal from './sections/personal.js';
import * as business from './sections/business.js';
import * as coding from './sections/coding.js';

const SECTIONS = { personal, business, coding };
const SECTION_ICON = { personal: 'user', business: 'briefcase', coding: 'code' };
const TAB_ICON = {
  chat: 'message', memory: 'brain', tasks: 'check-square', calendar: 'calendar', family: 'users', media: 'film',
  summary: 'sparkles', clients: 'briefcase', reminders: 'bell', statements: 'receipt', inbox: 'inbox', news: 'newspaper',
  simulate: 'flask', editor: 'code', scanner: 'shield-check', stress: 'gauge', github: 'github',
};
const JOB_ICON = { generation: 'message', insights: 'sparkles', reminder: 'bell', memory_wipe: 'database', global_reset: 'alert',
  news: 'newspaper', simulation: 'flask', vuln_scan: 'shield-check', scan: 'shield-check', locust: 'gauge', clone: 'github',
  sync: 'refresh', apply: 'code', summary: 'sparkles' };
const CORE = {
  chat: { id: 'chat', label: 'Chat', mount: chat.mount, unmount: chat.unmount },
  memory: { id: 'memory', label: 'Memory', mount: memory.mount, unmount: memory.unmount },
};
const REF_TABS = { task: 'tasks', event: 'calendar', client: 'clients', reminder: 'reminders', txn: 'statements', email: 'inbox' };
const app = { section: 'personal', tab: 'chat', chats: [], mounted: null, ui: {} };
const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

const tabsFor = (section) => [CORE.chat, ...(SECTIONS[section].tabs || []), CORE.memory];

// ------------------------------------------------------------------ React-like transitions
// Every in-app view change (chat, section, tab) goes through here: the top
// navigation bar sweeps left -> right while the view cross-fades.
let vtActive = false;
async function transition(update) {
  navBar.start();
  try {
    if (document.startViewTransition && !reduced && !vtActive) {
      try {
        vtActive = true;
        const vt = document.startViewTransition(update);
        // a skipped / interrupted transition rejects these - that's fine, the DOM update still happens
        vt.ready.catch(() => {});
        vt.finished.catch(() => {}).finally(() => { vtActive = false; });
        await vt.updateCallbackDone;
        return;
      } catch { vtActive = false; /* fall through */ }
    }
    await update();
    const v = $('#view');
    v.classList.remove('view-enter'); void v.offsetWidth; v.classList.add('view-enter');
  } finally {
    navBar.done();
  }
}

// ------------------------------------------------------------------ context passed to tabs
const ctx = {
  get section() { return app.section; },
  get me() { return state.me; },
  api, socket, toast, player, on, emit,
  openChat: (no) => openChat(no),
  openRef: (ref) => openRef(ref),
  setTab: (id) => setTab(id),
  refreshChats: () => loadChats(),
  saveUi: (k, v) => saveUi({ [k]: v }),
  ui: (k) => app.ui[k],
};
window.deminic = ctx;

const saveUi = debounce((obj) => { Object.assign(app.ui, obj); api('PUT', '/api/ui-state', app.ui, { idempotent: false }).catch(() => {}); }, 400);

// ------------------------------------------------------------------ indicators
function moveIndicator(container) {
  const ind = container.querySelector('.tab-indicator');
  const act = container.querySelector('.seg-item.active');
  if (!ind || !act) { if (ind) ind.style.width = '0px'; return; }
  ind.style.width = `${act.offsetWidth}px`;
  ind.style.transform = `translateX(${act.offsetLeft - 4}px)`;
  ind.style.left = '4px';
}

function paintSections() {
  const box = $('#section-switch');
  for (const b of $$('[data-section]', box)) b.classList.toggle('active', b.dataset.section === app.section);
  requestAnimationFrame(() => moveIndicator(box));
}

function paintTabs() {
  const host = $('#tabs');
  for (const b of $$('.seg-item', host)) b.remove();
  for (const t of tabsFor(app.section)) {
    const b = document.createElement('button');
    b.className = `seg-item ${t.id === app.tab ? 'active' : ''}`;
    b.dataset.tab = t.id;
    b.innerHTML = `${icon(TAB_ICON[t.id] || 'sparkles', 'w-3.5 h-3.5 opacity-80')}<span>${esc(t.label)}</span>`;
    b.setAttribute('role', 'tab');
    b.setAttribute('aria-selected', String(t.id === app.tab));
    b.addEventListener('click', () => setTab(t.id));
    host.appendChild(b);
  }
  requestAnimationFrame(() => { moveIndicator(host); host.querySelector('.seg-item.active')?.scrollIntoView({ block: 'nearest', inline: 'nearest' }); tabFade(); });
}

// Many tabs (Business has 9): fade the strip's edges while it scrolls instead of cutting a label in half.
function tabFade() {
  const host = $('#tabs');
  if (!host) return;
  const over = host.scrollWidth > host.clientWidth + 2;
  host.classList.toggle('is-overflowing', over);
  host.classList.toggle('at-start', host.scrollLeft < 4);
  host.classList.toggle('at-end', host.scrollLeft + host.clientWidth >= host.scrollWidth - 4);
}

async function mountTab() {
  const view = $('#view');
  if (app.mounted && app.mounted.unmount) { try { app.mounted.unmount(); } catch { /* ignore */ } }
  const tab = tabsFor(app.section).find((t) => t.id === app.tab) || CORE.chat;
  app.tab = tab.id;
  app.mounted = tab;
  await transition(async () => {
    view.innerHTML = '';
    const host = document.createElement('div');
    host.className = 'h-full';
    view.appendChild(host);
    try { await tab.mount(host, ctx); } catch (e) { console.error(e); host.innerHTML = `<div class="p-8 text-rose-600">Failed to load ${esc(tab.label)}: ${esc(e.message)}</div>`; }
  });
}

export async function setSection(section, { keepTab = false } = {}) {
  if (!SECTIONS[section]) return;
  app.section = section;
  if (!keepTab) app.tab = app.ui[`tab_${section}`] || 'chat';
  if (!tabsFor(section).some((t) => t.id === app.tab)) app.tab = 'chat';
  document.documentElement.dataset.section = section;
  paintSections();
  paintTabs();
  saveUi({ section });
  await loadChats();
  await mountTab();
}

async function setTab(id) {
  if (id === app.tab && app.mounted) return;
  app.tab = id;
  saveUi({ [`tab_${app.section}`]: id });
  paintTabs();
  await mountTab();
}

// ------------------------------------------------------------------ chat list
async function loadChats() {
  try { app.chats = (await api('GET', `/api/chats?section=${app.section}`)).chats; } catch { app.chats = []; }
  paintChats();
}

function paintChats() {
  const list = $('#chat-list');
  const q = ($('#chat-search').value || '').toLowerCase();
  const current = chat.currentChatNo(app.section);
  $('#chat-count').textContent = app.chats.length;
  list.innerHTML = '';
  const items = app.chats.filter((c) => !q || c.title.toLowerCase().includes(q) || c.ref.includes(q));
  if (!items.length) {
    list.innerHTML = `<div class="px-3 py-10 text-center"><span class="icon-badge-soft mx-auto">${icon(q ? 'search' : 'message', 'w-4 h-4')}</span>
      <p class="mt-3 text-xs text-slate-500">${q ? 'No matching chats' : 'No chats yet — say hello!'}</p></div>`;
    return;
  }
  for (const c of items) {
    const a = document.createElement('a');
    a.href = '#';
    a.className = `group nav-item ${c.chat_no === current ? 'active' : ''}`;
    a.innerHTML = `<span class="shrink-0 ${c.outdated ? 'text-rose-500' : 'text-slate-400'}">${icon(c.lawyer_mode ? 'scale' : 'message', 'w-4 h-4')}</span>
      <div class="min-w-0 flex-1"><div class="text-[13px] font-medium truncate ${c.outdated ? 'text-rose-600 dark:text-rose-400' : ''}">${esc(bizvars(c.title))}</div>
      <div class="text-[10px] text-slate-400 flex items-center gap-1.5"><span class="font-mono">${esc(c.ref)}</span>·<span>${esc(fmtRel(c.updated_at))}</span>
      ${c.outdated ? '<span class="badge-red !h-4">old business</span>' : ''}</div></div>
      ${c.generating ? '<span class="dot-typing text-accent-500" title="Generating in the background"><span></span><span></span><span></span></span>' : ''}
      <button class="opacity-0 group-hover:opacity-100 btn-plain btn-icon btn-sm" data-del title="Delete chat" aria-label="Delete chat">${icon('trash', 'w-3.5 h-3.5')}</button>`;
    a.addEventListener('click', async (e) => {
      e.preventDefault();
      if (e.target.closest('[data-del]')) {
        if (await confirmModal('Delete chat?', `${c.ref} "${c.title}" will be deleted permanently.`, { danger: true, confirmText: 'Delete' })) {
          await api('DELETE', `/api/chats/${c.chat_no}`);
          if (chat.currentChatNo(app.section) === c.chat_no) chat.clearCurrent(app.section);
          await loadChats();
          if (app.tab === 'chat') mountTab();
        }
        return;
      }
      closeSidebar();
      await openChat(c.chat_no);
    });
    list.appendChild(a);
  }
}

async function openChat(no, msgNo) {
  chat.setCurrent(app.section, no);
  saveUi({ [`chat_${app.section}`]: no });
  if (app.tab !== 'chat') { app.tab = 'chat'; paintTabs(); await mountTab(); } else { await transition(() => chat.open(no, msgNo)); }
  if (msgNo) chat.scrollToMessage(no, msgNo);
  paintChats();
}

async function newChat() {
  const r = await api('POST', '/api/chats', { section: app.section });
  app.chats.unshift(r.chat);
  closeSidebar();
  await openChat(r.chat.chat_no);
}

async function openRef(ref) {
  try {
    const r = await api('GET', `/api/refs/resolve?ref=${encodeURIComponent(ref)}`);
    if (r.section !== app.section) { app.section = r.section; paintSections(); await loadChats(); }
    if (r.type === 'chat') {
      chat.setCurrent(app.section, r.chat_no);
      app.tab = 'chat';
      paintTabs();
      await mountTab();
      if (r.msg_no) chat.scrollToMessage(r.chat_no, r.msg_no);
      paintChats();
    } else {
      const tab = r.type === 'email' && app.section === 'personal' ? 'family' : REF_TABS[r.type];
      app.tab = tabsFor(app.section).some((t) => t.id === tab) ? tab : 'chat';
      paintTabs();
      await mountTab();
      emit('ref:focus', r);
    }
  } catch (e) { toast(`${ref}: ${e.message}`, 'warn'); }
}

// ------------------------------------------------------------------ background tasks drawer
const jobs = new Map();
function paintJobs() {
  const list = $('#jobs-list');
  const all = [...jobs.values()].sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
  const running = all.filter((j) => ['running', 'scheduled'].includes(j.status));
  $('#jobs-count').textContent = `${running.length} running · ${all.length} total`;
  const badge = $('#jobs-badge');
  badge.textContent = running.length;
  badge.classList.toggle('hidden', !running.length);
  badge.classList.toggle('inline-flex', !!running.length);
  const color = { running: 'badge-blue', scheduled: 'badge-violet', completed: 'badge-green', cancelled: 'badge-gray', failed: 'badge-red', reverted: 'badge-amber' };
  list.innerHTML = all.length ? '' : `<div class="py-14 text-center"><span class="icon-badge-soft mx-auto">${icon('activity', 'w-4 h-4')}</span><p class="mt-3 text-sm text-slate-500">No background tasks</p></div>`;
  for (const j of all.slice(0, 80)) {
    const pct = Math.round((j.progress || 0) * 100);
    const live = ['running', 'scheduled'].includes(j.status);
    const div = document.createElement('div');
    div.className = 'card !p-3';
    div.innerHTML = `<div class="flex items-start gap-3"><span class="icon-badge-soft !w-8 !h-8 !rounded-lg mt-0.5">${icon(JOB_ICON[j.kind] || 'activity', 'w-4 h-4')}</span>
      <div class="min-w-0 flex-1"><div class="text-sm font-semibold truncate" title="${esc(j.title)}">${esc(bizvars(j.title))}</div>
        <div class="mt-1 flex flex-wrap items-center gap-1.5 text-[10px] text-slate-500"><span class="${color[j.status] || 'badge-gray'}">${esc(j.status)}</span>
          ${j.cancellable ? '' : `<span class="badge-amber" title="Protected by a restore point">${icon('shield', 'w-3 h-3')}protected</span>`}
          <span class="font-mono">${esc(j.request_id.slice(0, 8))}</span><span>${esc(fmtRel(j.created_at))}</span></div></div>
      ${j.can_cancel ? `<button class="btn-ghost btn-sm !text-rose-600" data-cancel="${esc(j.request_id)}">${icon('x', 'w-3.5 h-3.5')}Cancel</button>` : ''}</div>
      ${live ? `<div class="mt-2.5 h-1.5 rounded-full bg-slate-900/10 dark:bg-white/10 overflow-hidden"><div class="h-full rounded-full transition-all duration-500" data-w="${pct}"></div></div>` : ''}
      ${j.start_at && j.status === 'scheduled' ? `<div class="mt-1.5 text-[11px] text-slate-500 flex items-center gap-1">${icon('clock', 'w-3 h-3')}due ${esc(new Date(j.start_at).toLocaleString())}${j.cancel_deadline ? ` · cancellable until ${esc(new Date(j.cancel_deadline).toLocaleTimeString())}` : ''}</div>` : ''}
      ${j.detail ? `<div class="mt-1 text-[11px] text-slate-500 break-words">${esc(j.detail)}</div>` : ''}
      ${!j.can_cancel && j.cancel_reason && live ? `<div class="mt-1 text-[10px] text-amber-600">${esc(j.cancel_reason)}</div>` : ''}`;
    const bar = div.querySelector('[data-w]');
    if (bar) { bar.style.width = `${Math.max(4, Number(bar.dataset.w))}%`; bar.style.backgroundImage = 'var(--grad)'; }
    list.appendChild(div);
  }
  for (const b of $$('[data-cancel]', list)) {
    b.addEventListener('click', async () => {
      b.disabled = true;
      const ack = await cancelJob(b.dataset.cancel);
      toast(ack.ok ? 'Task cancelled' : `Not cancelled: ${ack.message}`, ack.ok ? 'ok' : 'warn');
    });
  }
}
async function loadJobs() {
  try { for (const j of (await api('GET', '/api/jobs')).jobs) jobs.set(j.request_id, j); } catch { /* ignore */ }
  paintJobs();
}
on('job', (j) => {
  const prev = jobs.get(j.request_id);
  jobs.set(j.request_id, j);
  paintJobs();
  if (prev && prev.status !== j.status && ['reverted', 'failed'].includes(j.status)) toast(`${j.title}: ${j.status} - ${j.detail}`, 'warn', 7000);
  if (j.kind === 'generation' && prev && prev.status !== j.status) {
    const c = app.chats.find((x) => x.chat_no === j.meta?.chat_no);
    if (c) { c.generating = j.status === 'running' ? j.request_id : null; paintChats(); }
  }
});

// ------------------------------------------------------------------ terminal + immersive + palette
function toggleTerminal(force) {
  const t = $('#terminal');
  const open = force !== undefined ? force : t.classList.contains('hidden');
  t.classList.toggle('hidden', !open);
  saveUi({ terminal_open: open ? '1' : '0' });
  if (open) { terminal.paint(); $('#term-input').focus(); }
}

function setImmersiveMode(on, announce = true) {
  setImmersive(on);
  $('#toggle-immersive').classList.toggle('text-accent-500', on);
  api('PUT', '/api/device-prefs', { immersive: on }, { idempotent: false }).catch(() => {});
  app.ui.immersive = on ? '1' : '0';
  if (announce) toast(on ? 'Immersive mode on - starry night in dark mode, birds & clouds in light mode' : 'Immersive mode off', 'info', 3000);
}
function setComfortMode(on) {
  setComfort(on);
  api('PUT', '/api/device-prefs', { vr_comfort: on }, { idempotent: false }).catch(() => {});
  app.ui.vr_comfort = on ? '1' : '0';
  requestAnimationFrame(() => { moveIndicator($('#tabs')); moveIndicator($('#section-switch')); });
}

function paletteItems() {
  const items = [];
  for (const s of Object.keys(SECTIONS)) items.push({ group: 'Sections', label: `${s[0].toUpperCase()}${s.slice(1)} mode`, icon: SECTION_ICON[s], run: () => setSection(s) });
  for (const t of tabsFor(app.section)) items.push({ group: `Tabs · ${app.section}`, label: t.label, icon: TAB_ICON[t.id], run: () => setTab(t.id) });
  for (const c of app.chats.slice(0, 25)) items.push({ group: 'Recent chats', label: bizvars(c.title), hint: c.ref, icon: 'message', keywords: c.ref, run: () => openChat(c.chat_no) });
  items.push(
    { group: 'Actions', label: 'New chat', icon: 'plus', hint: 'N', run: newChat },
    { group: 'Actions', label: 'Toggle dark mode', icon: 'moon', run: () => document.querySelector('[data-theme-toggle]').click() },
    { group: 'Actions', label: isImmersive() ? 'Turn immersive (VR) mode off' : 'Turn immersive (VR) mode on', icon: 'vr', run: () => setImmersiveMode(!isImmersive()) },
    { group: 'Actions', label: document.documentElement.classList.contains('vr-comfort') ? 'VR comfort layout off' : 'VR comfort layout on (larger)', icon: 'eye', run: () => setComfortMode(!document.documentElement.classList.contains('vr-comfort')) },
    { group: 'Actions', label: 'Open terminal', icon: 'terminal', hint: 'Ctrl+`', run: () => toggleTerminal(true) },
    { group: 'Actions', label: 'System status (live AI ping)', icon: 'radio', run: () => openStatus() },
    { group: 'Actions', label: 'Background tasks', icon: 'activity', run: () => { $('#jobs-drawer').classList.remove('translate-x-[110%]'); loadJobs(); } },
    { group: 'Navigate', label: 'Home page', icon: 'home', run: () => { location.href = '/'; } },
    { group: 'Navigate', label: 'Settings', icon: 'settings', run: () => { location.href = '/settings'; } },
    { group: 'Navigate', label: 'How it works (technical)', icon: 'layers', run: () => { location.href = '/technical'; } },
    { group: 'Navigate', label: 'Stress-test results', icon: 'gauge', run: () => { location.href = '/locust'; } },
    { group: 'Navigate', label: 'Sign out', icon: 'log-out', run: () => $('#logout').click() },
  );
  return items;
}

// ------------------------------------------------------------------ sidebar (mobile)
function openSidebar() { $('#sidebar').classList.remove('-translate-x-[110%]'); $('#sidebar-scrim').classList.remove('hidden'); }
function closeSidebar() { $('#sidebar').classList.add('-translate-x-[110%]'); $('#sidebar-scrim').classList.add('hidden'); }

// ------------------------------------------------------------------ boot
async function boot() {
  bindThemeToggles();
  state.me = await api('GET', '/api/me');
  app.ui = state.me.ui || {};
  for (const mod of Object.values(SECTIONS)) if (mod.registerCards) mod.registerCards(registerCard);
  for (const mod of Object.values(SECTIONS)) if (mod.init) { try { mod.init(ctx); } catch (e) { console.error(e); } }
  chat.init(ctx);
  installIconUpgrader($('#view'), $('#jobs-drawer'), $('#modal-root'), $('#player'));

  terminal.initTerminal({
    section: () => app.section,
    tabIds: () => tabsFor(app.section).map((t) => t.id),
    recentChats: () => app.chats.slice(0, 12),
    runningJobs: () => [...jobs.values()].filter((j) => j.can_cancel),
    allJobs: () => [...jobs.values()],
    whoami: () => `${state.me.user.name} (@${state.me.user.username}) · AI: ${state.me.user.ai_name} · section ${app.section}`,
    cancel: async (prefix) => {
      const j = [...jobs.values()].find((x) => x.request_id.startsWith(prefix));
      return j ? cancelJob(j.request_id) : { ok: false, message: 'no such task' };
    },
    theme: (t) => { applyTheme(t); api('PUT', '/api/theme', { theme: t }, { idempotent: false }).catch(() => {}); },
    section: (s) => setSection(s), tab: (t) => setTab(t), openRef: (r) => openRef(r), newChat,
    immersive: (onOff) => setImmersiveMode(onOff), comfort: (onOff) => setComfortMode(onOff),
    palette: () => openPalette(paletteItems),
  });

  const s = socket();
  on('socket:state', ({ connected }) => {
    $('#conn-dot').className = `live-dot ${connected ? 'bg-emerald-500' : 'bg-rose-500'}`;
    $('#conn-text').textContent = connected ? 'live' : 'offline';
    if (connected) {
      s.emit('terminal:history', {}, (r) => { if (r && r.ok) terminal.setHistory(r.lines); });
      loadJobs();
    }
  });
  on('reminder', (r) => { toast(`⏰ ${r.title || 'Reminder'}${r.when ? ' · ' + r.when : ''}`, 'warn', 12000); if (window.Notification && Notification.permission === 'granted') new Notification(r.title || 'Reminder', { body: r.body || '' }); });
  on('notify', (n) => toast(n.message || '', n.kind || 'info', n.ms || 5000));
  on('player:play', (p) => { if (p.kind === 'audio') player.playAudio(p); else if (p.kind === 'youtube') player.playYouTube(p); });
  on('chat:done', (d) => {
    if (d.section === app.section) loadChats();
    if (!(app.tab === 'chat' && chat.currentChatNo(app.section) === d.chat_no)) toast(`Reply ready in chat#${d.chat_no}`, 'ok');
  });

  for (const b of $$('#section-switch [data-section]')) b.addEventListener('click', () => { closeSidebar(); setSection(b.dataset.section); });
  $('#new-chat').addEventListener('click', newChat);
  $('#chat-search').addEventListener('input', debounce(paintChats, 150));
  $('#open-jobs').addEventListener('click', () => { $('#jobs-drawer').classList.remove('translate-x-[110%]'); loadJobs(); });
  $('#close-jobs').addEventListener('click', () => $('#jobs-drawer').classList.add('translate-x-[110%]'));
  $('#toggle-terminal').addEventListener('click', () => toggleTerminal());
  $('#term-close').addEventListener('click', () => toggleTerminal(false));
  $('#toggle-immersive').addEventListener('click', () => setImmersiveMode(!isImmersive()));
  $('#open-palette').addEventListener('click', () => openPalette(paletteItems));
  $('#open-sidebar').addEventListener('click', openSidebar);
  for (const b of $$('[data-close-sidebar]')) b.addEventListener('click', closeSidebar);
  $('#logout').addEventListener('click', async () => { const r = await api('POST', '/api/auth/logout'); location.href = r.redirect || '/login'; });
  document.addEventListener('click', (e) => {
    const a = e.target.closest('[data-ref]');
    if (a) { e.preventDefault(); openRef(a.dataset.ref); }
  });
  document.addEventListener('keydown', (e) => {
    const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || '') || document.activeElement?.closest('.CodeMirror');
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(paletteItems); }
    else if ((e.ctrlKey || e.metaKey) && e.key === '`') { e.preventDefault(); toggleTerminal(); }
    else if (!typing && !e.ctrlKey && !e.metaKey && !e.altKey && e.key.toLowerCase() === 'n') { e.preventDefault(); newChat(); }
  });
  window.addEventListener('resize', debounce(() => { moveIndicator($('#tabs')); moveIndicator($('#section-switch')); tabFade(); }, 100));
  const tabsEl = $('#tabs');
  tabsEl.addEventListener('scroll', tabFade, { passive: true });
  // a mouse wheel scrolls the tab strip sideways when it overflows
  tabsEl.addEventListener('wheel', (e) => {
    if (Math.abs(e.deltaY) > Math.abs(e.deltaX) && tabsEl.scrollWidth > tabsEl.clientWidth) { tabsEl.scrollLeft += e.deltaY; e.preventDefault(); }
  }, { passive: false });
  if (window.Notification && Notification.permission === 'default') setTimeout(() => Notification.requestPermission().catch(() => {}), 4000);

  // immersive / VR: honour saved prefs; on a Meta Quest suggest it once
  if (app.ui.immersive === '1' && !isImmersive()) setImmersive(true);
  $('#toggle-immersive').classList.toggle('text-accent-500', isImmersive());
  if (isQuest() && app.ui.immersive === undefined) { setImmersiveMode(true, false); toast('Meta Quest detected - immersive mode + comfort layout enabled', 'ok', 5000); }

  const startSection = app.ui.section && SECTIONS[app.ui.section] ? app.ui.section : 'personal';
  for (const sec of Object.keys(SECTIONS)) { const n = Number(app.ui[`chat_${sec}`]); if (n) chat.setCurrent(sec, n); }
  await setSection(startSection);
  if (app.ui.terminal_open === '1') toggleTerminal(true);
}

boot().catch((e) => { console.error(e); toast(`Failed to start: ${e.message}`, 'error', 10000); });
