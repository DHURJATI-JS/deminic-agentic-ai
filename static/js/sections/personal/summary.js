// Personal AI summary - mind map of life areas, challenges, goals, wins and
// suggestions (Summary rows with type="personal", encrypted at rest).
import { $, api, busy, toast } from '../../core.js';
import { esc, fmtDate, refLink } from './util.js';

let root = null;
let C = null;
let running = null;
const unsubs = [];
const NS = 'http://www.w3.org/2000/svg';
const COLORS = ['#0A84FF', '#BF5AF2', '#FF9F0A', '#30D158', '#FF375F', '#64D2FF', '#FFD60A'];

export async function mount(el, ctx) {
  root = el; C = ctx;
  el.innerHTML = `<div class="h-full overflow-y-auto scroll-thin p-5 md:p-8">
    <div class="flex flex-wrap items-center gap-3">
      <div><h2 class="text-xl font-semibold">Personal AI summary</h2>
        <p class="text-sm text-slate-500">Built from what you tell ${esc(ctx.me.user.ai_name)} - private, encrypted at rest.</p></div>
      <span class="flex-1"></span><span id="s-state" class="text-xs text-slate-500"></span>
      <button class="btn-primary" id="s-regen">Regenerate from recent chats</button>
    </div>
    <div id="s-body" class="mt-5"><p class="text-sm text-slate-500">Loading…</p></div></div>`;
  $('#s-regen', el).addEventListener('click', regenerate);
  unsubs.push(ctx.on('summary:updated', (d) => { if (d.type === 'personal') load(); }));
  unsubs.push(ctx.on('job', (j) => {
    if (j.kind === 'summary' && j.meta?.type === 'personal') {
      running = ['running', 'scheduled'].includes(j.status) ? j.request_id : null;
      paintState(j);
      if (!running) load();
    }
  }));
  await load();
}

export function unmount() { while (unsubs.length) unsubs.pop()(); root = null; }

function paintState(j) {
  const s = root && $('#s-state', root);
  if (!s) return;
  const btn = $('#s-regen', root);
  if (j && ['running', 'scheduled'].includes(j.status)) { s.textContent = `${j.detail || 'working'}…`; btn.disabled = true; }
  else { s.textContent = j && j.status !== 'completed' ? `${j.status}: ${j.detail || ''}` : ''; btn.disabled = false; }
}

async function regenerate(e) {
  busy(e.currentTarget, true, 'Starting');
  try {
    const r = await api('POST', '/api/personal/summary/regenerate');
    running = r.request_id;
    toast('Regenerating your summary in the background', 'ok');
    paintState({ status: 'running', detail: 'gathering recent chats' });
  } catch (ex) { toast(ex.message, ex.status === 409 ? 'warn' : 'error'); busy($('#s-regen', root), false); }
}

async function load() {
  if (!root) return;
  let r;
  try { r = await api('GET', '/api/personal/summary'); } catch (e) { $('#s-body', root).innerHTML = `<p class="text-rose-600">${esc(e.message)}</p>`; return; }
  running = r.running;
  const btn = $('#s-regen', root);
  btn.disabled = !!running;
  if (btn.dataset.label) btn.innerHTML = btn.dataset.label;
  const body = $('#s-body', root);
  const cur = r.current;
  if (!cur) {
    body.innerHTML = `<div class="card text-center py-12"><div class="text-4xl">🧠</div><p class="mt-3 text-sm text-slate-500 max-w-md mx-auto">No summary yet. Talk about what's on your mind in Personal chats (for example
      <i>"I'm stressed about my exams"</i> or <i>"my goal is to get fit"</i>) - or press Regenerate.</p></div>`;
    return;
  }
  const d = cur.data || {};
  body.innerHTML = `<div class="grid xl:grid-cols-[1.4fr_1fr] gap-5 items-start">
      <div class="panel !rounded-2xl p-3"><div class="flex items-center gap-2 px-2 pt-1"><h3 class="font-semibold text-sm">Mind map</h3>
        ${d.mood ? `<span class="chip">mood: ${esc(d.mood)}</span>` : ''}<span class="flex-1"></span><span class="text-[11px] text-slate-500">${esc(fmtDate(cur.created_at))}${cur.ref ? ' · ' + refLink(cur.ref) : ''}</span></div>
        <div id="s-map" class="w-full overflow-x-auto"></div></div>
      <div class="space-y-4">
        <div class="card"><h3 class="font-semibold text-sm">Summary</h3><p class="mt-2 text-sm leading-relaxed text-slate-700 dark:text-slate-200">${esc(cur.content)}</p></div>
        ${list('Challenges', d.challenges, 'text-rose-600 dark:text-rose-400', '⚠️')}
        ${list('Goals', d.goals, 'text-emerald-600 dark:text-emerald-400', '🎯')}
        ${list('Wins', d.wins, 'text-amber-600 dark:text-amber-400', '🏆')}
        ${list('Suggestions', d.suggestions, 'text-accent-600 dark:text-accent-300', '💡')}
      </div></div>
    ${r.history.length ? `<details class="mt-6 card"><summary class="cursor-pointer text-sm font-semibold">Earlier summaries (${r.history.length})</summary>
      <div class="mt-3 space-y-2">${r.history.map((h) => `<div class="rounded-xl glass-soft p-3 text-sm"><div class="text-[11px] text-slate-500">${esc(fmtDate(h.created_at))}${h.ref ? ' · ' + refLink(h.ref) : ''}</div>${esc(h.content)}</div>`).join('')}</div></details>` : ''}`;
  $('#s-map', root).appendChild(mindMap(d.mind_map || { center: C.me.user.name, branches: [] }));
}

function list(title, items, cls, icon) {
  if (!items || !items.length) return '';
  return `<div class="card"><h3 class="font-semibold text-sm ${cls}">${icon} ${esc(title)}</h3><ul class="mt-2 space-y-1.5 text-sm">${items.map((i) => `<li class="flex gap-2"><span class="text-slate-400">•</span><span>${esc(i)}</span></li>`).join('')}</ul></div>`;
}

function svg(tag, attrs, parent) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  if (parent) parent.appendChild(n);
  return n;
}

function wrapText(text, max = 18) {
  const words = String(text || '').split(/\s+/);
  const lines = [];
  let cur = '';
  for (const w of words) {
    if ((cur + ' ' + w).trim().length > max && cur) { lines.push(cur); cur = w; } else cur = (cur + ' ' + w).trim();
  }
  if (cur) lines.push(cur);
  return lines.slice(0, 3);
}

function label(parent, x, y, text, { size = 12, weight = 500, fill = 'currentColor', max = 18 } = {}) {
  const lines = wrapText(text, max);
  const t = svg('text', { x, y: y - ((lines.length - 1) * size * 0.6), 'text-anchor': 'middle', 'font-size': size, 'font-weight': weight, fill }, parent);
  lines.forEach((ln, i) => { const s = svg('tspan', { x, dy: i === 0 ? 0 : size * 1.2 }, t); s.textContent = ln; });
  return lines.length;
}

/** Radial mind map built with DOM APIs (CSP-safe, theme aware via currentColor). */
export function mindMap(map) {
  const W = 760; const H = 560; const cx = W / 2; const cy = H / 2;
  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, class: 'w-full min-w-[560px] h-auto text-slate-800 dark:text-slate-100', role: 'img', 'aria-label': 'Mind map' });
  const branches = (map.branches || []).slice(0, 7);
  const n = Math.max(branches.length, 1);
  const R1 = 165; const R2 = 255;
  const lines = svg('g', { 'stroke-linecap': 'round' }, root);
  const nodes = svg('g', {}, root);
  branches.forEach((b, i) => {
    const color = COLORS[i % COLORS.length];
    const a = (-Math.PI / 2) + (2 * Math.PI * i) / n;
    const bx = cx + R1 * Math.cos(a); const by = cy + R1 * Math.sin(a);
    svg('path', { d: `M${cx},${cy} Q${cx + (R1 * 0.5) * Math.cos(a - 0.25)},${cy + (R1 * 0.5) * Math.sin(a - 0.25)} ${bx},${by}`, stroke: color, 'stroke-width': 3, fill: 'none', opacity: 0.75 }, lines);
    const kids = (b.children || []).slice(0, 5);
    const spread = Math.min(0.9, (2 * Math.PI / n) * 0.8);
    kids.forEach((k, j) => {
      const ka = a + (kids.length > 1 ? (-spread / 2 + (spread * j) / (kids.length - 1)) : 0);
      const kx = cx + R2 * Math.cos(ka); const ky = cy + R2 * Math.sin(ka);
      svg('line', { x1: bx, y1: by, x2: kx, y2: ky, stroke: color, 'stroke-width': 1.5, opacity: 0.5 }, lines);
      svg('circle', { cx: kx, cy: ky, r: 4, fill: color }, nodes);
      const below = Math.sin(ka) >= 0;
      label(nodes, kx, ky + (below ? 18 : -12), k, { size: 10.5, weight: 400, max: 20 });
    });
    svg('rect', { x: bx - 64, y: by - 20, width: 128, height: 40, rx: 14, fill: color, opacity: 0.16, stroke: color, 'stroke-width': 1.5 }, nodes);
    label(nodes, bx, by + 4, b.label, { size: 12, weight: 600, max: 16 });
  });
  svg('circle', { cx, cy, r: 62, fill: '#0A84FF', opacity: 0.18 }, nodes);
  svg('circle', { cx, cy, r: 54, fill: '#0A84FF', opacity: 0.9 }, nodes);
  label(nodes, cx, cy + 5, map.center || 'You', { size: 13, weight: 700, fill: '#ffffff', max: 12 });
  return root;
}
