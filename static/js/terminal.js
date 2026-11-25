// In-app terminal: live per-account logs + commands with Tab autocomplete,
// ghost-text completion, history and AI-suggested commands ("?" + plain English).
import { $, api, esc, on } from './core.js';

const T = { lines: [], filter: '', history: [], hIdx: -1, sugg: [], active: 0, aiBusy: false, app: null };

export const COMMANDS = [
  { name: 'help', usage: 'help', desc: 'list all commands' },
  { name: 'jobs', usage: 'jobs', desc: 'list background tasks' },
  { name: 'cancel', usage: 'cancel <request-id>', desc: 'cancel a cancellable background task', args: () => T.app.runningJobs().map((j) => ({ v: j.request_id.slice(0, 8), d: j.title })) },
  { name: 'clear', usage: 'clear', desc: 'clear the terminal' },
  { name: 'whoami', usage: 'whoami', desc: 'show the signed-in account' },
  { name: 'status', usage: 'status', desc: 'health of database, Redis, ClamAV, AI gateway' },
  { name: 'theme', usage: 'theme light|dark|system', desc: 'switch colour theme', args: () => ['light', 'dark', 'system'].map((v) => ({ v })) },
  { name: 'section', usage: 'section personal|business|coding', desc: 'switch section', args: () => ['personal', 'business', 'coding'].map((v) => ({ v })) },
  { name: 'tab', usage: 'tab <name>', desc: 'open a tab of the current section', args: () => T.app.tabIds().map((v) => ({ v })) },
  { name: 'open', usage: 'open chat#N', desc: 'open a chat or any reference', args: () => T.app.recentChats().map((c) => ({ v: c.ref, d: c.title })) },
  { name: 'new', usage: 'new chat', desc: 'start a new chat in this section', args: () => [{ v: 'chat' }] },
  { name: 'immersive', usage: 'immersive on|off', desc: 'immersive / VR background', args: () => [{ v: 'on' }, { v: 'off' }] },
  { name: 'comfort', usage: 'comfort on|off', desc: 'larger VR-comfort layout', args: () => [{ v: 'on' }, { v: 'off' }] },
  { name: 'filter', usage: 'filter <text>', desc: 'filter visible log lines' },
  { name: 'palette', usage: 'palette', desc: 'open the command palette' },
  { name: 'ask', usage: 'ask <what you want to do>', desc: 'let the AI suggest a command' },
];

function line(l) {
  const colors = { info: 'text-emerald-200', warn: 'text-amber-300', error: 'text-rose-300', debug: 'text-slate-400', ai: 'text-sky-300' };
  const d = document.createElement('div');
  d.className = colors[l.level] || colors.info;
  d.textContent = `${(l.ts || '').slice(11, 19)} [${l.src || 'app'}] ${l.msg}`;
  return d;
}

export function paint() {
  const out = $('#term-out');
  out.innerHTML = '';
  const f = T.filter.toLowerCase();
  for (const l of T.lines.slice(-500)) if (!f || `${l.src} ${l.msg}`.toLowerCase().includes(f)) out.appendChild(line(l));
  out.scrollTop = out.scrollHeight;
}

export function setHistory(lines) { T.lines = lines || []; paint(); }
export function sys(msg, level = 'info') { T.lines.push({ ts: new Date().toISOString(), level, src: 'terminal', msg }); paint(); }

// ------------------------------------------------------------------ suggestions
function compute(text) {
  const raw = text.replace(/^\s+/, '');
  if (!raw) return COMMANDS.slice(0, 8).map((c) => ({ value: c.name + (c.usage.includes(' ') ? ' ' : ''), label: c.usage, desc: c.desc }));
  if (raw.startsWith('?')) return [{ value: raw, label: `Ask AI: "${raw.slice(1).trim() || '...'}"`, desc: 'press Enter / Tab', ai: true }];
  const [head, ...rest] = raw.split(/\s+/);
  const cmd = COMMANDS.find((c) => c.name === head.toLowerCase());
  if (cmd && (rest.length || raw.endsWith(' '))) {
    const partial = rest.join(' ').toLowerCase();
    const opts = (cmd.args ? cmd.args() : []).filter((a) => a.v.toLowerCase().startsWith(partial));
    return opts.slice(0, 8).map((a) => ({ value: `${cmd.name} ${a.v}`, label: `${cmd.name} ${a.v}`, desc: a.d || cmd.desc }));
  }
  const matches = COMMANDS.filter((c) => c.name.startsWith(head.toLowerCase()));
  if (matches.length) return matches.map((c) => ({ value: c.name + (c.usage.includes(' ') ? ' ' : ''), label: c.usage, desc: c.desc }));
  if (raw.split(/\s+/).length >= 2) return [{ value: `?${raw}`, label: `Ask AI which command does: "${raw}"`, desc: 'Tab to ask', ai: true }];
  return [];
}

function render() {
  const box = $('#term-suggest');
  const input = $('#term-input');
  const ghost = $('#term-ghost');
  const top = T.sugg[T.active];
  ghost.textContent = top && !top.ai && top.value.toLowerCase().startsWith(input.value.toLowerCase()) && input.value
    ? input.value + top.value.slice(input.value.length) : '';
  if (!T.sugg.length || document.activeElement !== input) { box.classList.add('hidden'); return; }
  box.classList.remove('hidden');
  box.innerHTML = T.sugg.map((s, i) => `<div class="item ${i === T.active ? 'active' : ''}" data-i="${i}" role="option">
    <span class="${s.ai ? 'text-sky-300' : 'text-emerald-300'}">${s.ai ? '✨' : '›'}</span><span class="flex-1 truncate">${esc(s.label)}</span>
    <span class="text-slate-500 truncate max-w-[45%]">${esc(s.desc || '')}</span></div>`).join('');
}

function refresh() { T.sugg = compute($('#term-input').value); T.active = 0; render(); }

async function askAI(text) {
  if (T.aiBusy) return;
  T.aiBusy = true;
  sys(`asking the AI for a command: "${text}"`, 'ai');
  try {
    const r = await api('POST', '/api/terminal/suggest', {
      text, commands: COMMANDS.map((c) => `${c.usage}  # ${c.desc}`),
      context: { section: T.app.section(), tabs: T.app.tabIds(), running_jobs: T.app.runningJobs().length },
    });
    const list = r.suggestions || [];
    if (!list.length) { sys('the AI had no matching command - type help', 'warn'); return; }
    T.sugg = list.map((s) => ({ value: s.command, label: s.command, desc: s.why }));
    T.active = 0;
    $('#term-input').value = list[0].command;
    $('#term-input').focus();
    render();
    sys(`AI suggests: ${list.map((s) => s.command).join('  |  ')}  (Enter to run, Tab/↑↓ to pick)`, 'ai');
  } catch (e) { sys(`AI suggestion failed: ${e.message}`, 'error'); } finally { T.aiBusy = false; }
}

async function run(cmdline) {
  const text = cmdline.trim();
  if (!text) return;
  T.history.unshift(text); T.history = T.history.slice(0, 60); T.hIdx = -1;
  if (text.startsWith('?')) { await askAI(text.slice(1).trim()); return; }
  const [c, ...args] = text.split(/\s+/);
  const a0 = (args[0] || '').toLowerCase();
  sys(`$ ${text}`, 'debug');
  const A = T.app;
  switch (c.toLowerCase()) {
    case 'help': for (const k of COMMANDS) sys(`${k.usage.padEnd(36)} ${k.desc}`); break;
    case 'clear': T.lines = []; paint(); break;
    case 'whoami': sys(A.whoami()); break;
    case 'jobs': { const js = A.allJobs(); if (!js.length) sys('no background tasks'); for (const j of js) sys(`${j.request_id.slice(0, 8)}  ${j.status.padEnd(10)} ${j.kind.padEnd(12)} ${j.title}`); break; }
    case 'cancel': { const ack = await A.cancel(a0); sys(ack.message, ack.ok ? 'info' : 'warn'); break; }
    case 'status': { const h = await api('GET', '/healthz'); sys(Object.entries(h).map(([k, v]) => `${k}:${v ? 'ok' : 'DOWN'}`).join('  ')); break; }
    case 'theme': if (['light', 'dark', 'system'].includes(a0)) A.theme(a0); else sys('usage: theme light|dark|system', 'warn'); break;
    case 'section': if (['personal', 'business', 'coding'].includes(a0)) A.section(a0, true); else sys('usage: section personal|business|coding', 'warn'); break;
    case 'tab': if (A.tabIds().includes(a0)) A.tab(a0); else sys(`tabs here: ${A.tabIds().join(', ')}`, 'warn'); break;
    case 'open': A.openRef(args[0] || ''); break;
    case 'new': A.newChat(); break;
    case 'immersive': A.immersive(a0 !== 'off'); break;
    case 'comfort': A.comfort(a0 !== 'off'); break;
    case 'filter': T.filter = args.join(' '); $('#term-filter').value = T.filter; paint(); break;
    case 'palette': A.palette(); break;
    case 'ask': await askAI(args.join(' ')); break;
    default:
      if (text.split(/\s+/).length >= 2) await askAI(text);
      else sys(`unknown command: ${c} - type help, or "?" + what you want to do`, 'warn');
  }
}

export function initTerminal(app) {
  T.app = app;
  const input = $('#term-input');
  on('log', (l) => {
    T.lines.push(l);
    if (T.lines.length > 900) T.lines.splice(0, 250);
    if ($('#terminal').classList.contains('hidden')) return;
    const f = T.filter.toLowerCase();
    if (f && !`${l.src} ${l.msg}`.toLowerCase().includes(f)) return;
    const out = $('#term-out');
    const stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 30;
    out.appendChild(line(l));
    if (stick) out.scrollTop = out.scrollHeight;
  });
  input.addEventListener('input', refresh);
  input.addEventListener('focus', refresh);
  input.addEventListener('blur', () => setTimeout(render, 120));
  input.addEventListener('keydown', async (e) => {
    if (e.key === 'Tab') {
      e.preventDefault();
      const s = T.sugg[T.active];
      if (s?.ai) { await askAI(s.value.replace(/^\?/, '').trim()); return; }
      if (s) { input.value = s.value; refresh(); }
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const dir = e.key === 'ArrowDown' ? 1 : -1;
      if (T.sugg.length > 1 && input.value) { T.active = (T.active + dir + T.sugg.length) % T.sugg.length; render(); return; }
      T.hIdx = Math.max(-1, Math.min(T.history.length - 1, T.hIdx - dir));
      input.value = T.hIdx >= 0 ? T.history[T.hIdx] : '';
      refresh();
    } else if (e.key === 'Escape') { T.sugg = []; render(); }
  });
  $('#term-suggest').addEventListener('mousedown', (e) => {
    const el = e.target.closest('[data-i]');
    if (!el) return;
    e.preventDefault();
    const s = T.sugg[Number(el.dataset.i)];
    if (s.ai) askAI(s.value.replace(/^\?/, '').trim()); else { input.value = s.value; refresh(); input.focus(); }
  });
  $('#term-form').addEventListener('submit', async (e) => { e.preventDefault(); const v = input.value; input.value = ''; T.sugg = []; render(); await run(v); });
  $('#term-filter').addEventListener('input', (e) => { T.filter = e.target.value; paint(); });
  $('#term-clear').addEventListener('click', () => { T.lines = []; paint(); });
}
