// Calendar tab - month grid + day agenda; events carry an "Add to Google
// Calendar" link, an .ics download, and the paste-your-Meet-link flow.
import { $, $$, api, confirmModal, debounce, modal, toast } from '../../core.js';
import { esc, flash, formNode, formValues, refLink, sameDay, timeOnly } from './util.js';

let root = null;
let C = null;
let month = null; // Date - first day of the visible month
let selected = null; // Date
let data = { events: [], tasks: [] };
let agenda = { events: [], tasks: [] };
const unsubs = [];
const WEEK = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

const ymd = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
function gridStart(m) { const d = new Date(m); const wd = (d.getDay() + 6) % 7; d.setDate(d.getDate() - wd); return d; }

export async function mount(el, ctx) {
  root = el; C = ctx;
  const now = new Date();
  month = new Date(now.getFullYear(), now.getMonth(), 1);
  selected = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  el.innerHTML = `<div class="h-full overflow-y-auto scroll-thin p-5 md:p-8">
    <div class="flex flex-wrap items-center gap-3">
      <h2 class="text-xl font-semibold min-w-[11rem]" id="cal-title"></h2>
      <div class="flex gap-1"><button class="btn-ghost btn-sm" data-nav="-1" aria-label="Previous month">‹</button>
        <button class="btn-ghost btn-sm" data-nav="0">Today</button><button class="btn-ghost btn-sm" data-nav="1" aria-label="Next month">›</button></div>
      <span class="flex-1"></span>
      <button class="btn-primary" id="cal-new">New event</button>
    </div>
    <p class="mt-1 text-sm text-slate-500">Events get an <b>Add to Google Calendar</b> link and an .ics file. Need a Google Meet? Create one at meet.google.com/new and paste it - ${esc(ctx.me.user.ai_name)} attaches it.</p>
    <div class="mt-5 grid xl:grid-cols-[1fr_380px] gap-5 items-start">
      <div class="panel !rounded-2xl p-2 md:p-3">
        <div class="grid grid-cols-7 text-[11px] text-slate-500 font-medium mb-1 text-center">${WEEK.map((w) => `<div>${w}</div>`).join('')}</div>
        <div id="cal-grid" class="grid grid-cols-7 gap-1"></div>
      </div>
      <aside class="space-y-4">
        <div class="card"><h3 class="font-semibold text-sm" id="cal-day-title"></h3><div id="cal-day" class="mt-3 space-y-2"></div></div>
        <div class="card"><h3 class="font-semibold text-sm">Next 14 days</h3><div id="cal-agenda" class="mt-3 space-y-2"></div></div>
      </aside>
    </div></div>`;
  for (const b of $$('[data-nav]', el)) b.addEventListener('click', () => {
    const n = Number(b.dataset.nav);
    if (n === 0) { const t = new Date(); month = new Date(t.getFullYear(), t.getMonth(), 1); selected = new Date(t.getFullYear(), t.getMonth(), t.getDate()); }
    else month = new Date(month.getFullYear(), month.getMonth() + n, 1);
    load();
  });
  $('#cal-new', el).addEventListener('click', () => newEvent());
  const reload = debounce(load, 300);
  unsubs.push(ctx.on('calendar:changed', reload));
  unsubs.push(ctx.on('task:changed', reload));
  unsubs.push(ctx.on('ref:focus', (r) => { if (r.type === 'event') focusEvent(r.no); }));
  await load();
}

export function unmount() { while (unsubs.length) unsubs.pop()(); root = null; }

async function load() {
  if (!root) return;
  const gs = gridStart(month);
  const ge = new Date(gs); ge.setDate(ge.getDate() + 42);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const ae = new Date(today); ae.setDate(ae.getDate() + 14);
  try {
    [data, agenda] = await Promise.all([
      api('GET', `/api/personal/calendar?start=${ymd(gs)}&end=${ymd(ge)}`),
      api('GET', `/api/personal/calendar?start=${ymd(today)}&end=${ymd(ae)}`),
    ]);
  } catch (e) { toast(e.message, 'error'); return; }
  paintGrid();
  paintDay();
  paintAgenda();
}

function itemsOn(day, src = data) {
  const ev = src.events.filter((e) => sameDay(new Date(e.starts_at), day));
  const tk = src.tasks.filter((t) => t.due_at && sameDay(new Date(t.due_at), day));
  return { ev, tk };
}

function paintGrid() {
  $('#cal-title', root).textContent = month.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
  const grid = $('#cal-grid', root);
  grid.innerHTML = '';
  const d = gridStart(month);
  const today = new Date();
  for (let i = 0; i < 42; i++) {
    const day = new Date(d); day.setDate(d.getDate() + i);
    const { ev, tk } = itemsOn(day);
    const cell = document.createElement('button');
    const inMonth = day.getMonth() === month.getMonth();
    cell.className = `min-h-[78px] md:min-h-[96px] rounded-xl p-1.5 text-left align-top transition border ${sameDay(day, selected) ? 'border-accent-500/70 bg-accent-500/10' : 'border-transparent hover:bg-white/50 dark:hover:bg-white/[0.05]'} ${inMonth ? '' : 'opacity-45'}`;
    const pills = [...ev.map((e) => `<div class="truncate rounded-md px-1.5 py-0.5 text-[10px] bg-accent-500/15 text-accent-700 dark:text-accent-300">${esc(timeOnly(e.starts_at))} ${esc(e.title)}</div>`),
      ...tk.map((t) => `<div class="truncate rounded-md px-1.5 py-0.5 text-[10px] ${t.status === 'done' ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 line-through' : 'bg-violet-500/15 text-violet-700 dark:text-violet-300'}">✓ ${esc(t.title)}</div>`)];
    cell.innerHTML = `<div class="text-xs font-semibold ${sameDay(day, today) ? 'inline-flex items-center justify-center w-6 h-6 rounded-full bg-accent-500 text-white' : ''}">${day.getDate()}</div>
      <div class="mt-1 space-y-0.5">${pills.slice(0, 3).join('')}${pills.length > 3 ? `<div class="text-[10px] text-slate-500">+${pills.length - 3} more</div>` : ''}</div>`;
    cell.addEventListener('click', () => { selected = day; paintGrid(); paintDay(); });
    grid.appendChild(cell);
  }
}

function eventCard(e) {
  const div = document.createElement('div');
  div.className = 'rounded-xl glass-soft p-3 text-sm';
  div.dataset.eventNo = e.event_no;
  div.innerHTML = `<div class="flex items-start gap-2"><div class="min-w-0 flex-1"><div class="font-medium break-words">${esc(e.title)}</div>
      <div class="text-[11px] text-slate-500 mt-0.5">${esc(e.start_local)} → ${esc(timeOnly(e.ends_at))}${e.location ? ' · 📍 ' + esc(e.location) : ''}</div>
      <div class="text-[11px] mt-0.5">${refLink(e.ref)} ${e.source_ref ? '· from ' + refLink(e.source_ref) : ''} ${e.stale ? '<span class="badge-amber">stale</span>' : ''}</div></div>
      <button class="btn-plain btn-icon btn-sm text-rose-600" data-act="delete" title="Delete event" aria-label="Delete event">&times;</button></div>
    ${e.description ? `<p class="mt-1.5 text-xs text-slate-600 dark:text-slate-300 break-words">${esc(e.description)}</p>` : ''}
    ${meetBlock(e)}
    <div class="mt-2 flex flex-wrap gap-1.5">
      <a class="btn-ghost btn-sm no-underline" href="${esc(e.google_url)}" target="_blank" rel="noopener noreferrer">Add to Google Calendar</a>
      <a class="btn-ghost btn-sm no-underline" href="${esc(e.ics_url)}" download>.ics</a></div>`;
  bindMeet(div, e);
  div.querySelector('[data-act="delete"]').addEventListener('click', async () => {
    if (!(await confirmModal('Delete event?', `${e.ref} "${e.title}" will be removed.`, { danger: true, confirmText: 'Delete' }))) return;
    await api('DELETE', `/api/personal/events/${e.event_no}`);
    toast('Event deleted', 'ok');
    load();
  });
  return div;
}

export function meetBlock(e) {
  if (e.meet_link) return `<div class="mt-2 text-xs"><a href="${esc(e.meet_link)}" target="_blank" rel="noopener noreferrer" class="btn-success btn-sm no-underline">🎥 Join Google Meet</a> <span class="font-mono text-slate-500">${esc(e.meet_link.replace('https://', ''))}</span></div>`;
  if (e.needs_meet_link) return `<form class="meet-form mt-2 rounded-xl bg-amber-500/10 border border-amber-500/25 p-2.5 text-xs space-y-2">
      <div>🎥 This event needs a Google Meet link. I can't create Meet links - <a href="${esc(e.meet_new_url)}" target="_blank" rel="noopener noreferrer">create one here</a> and paste it:</div>
      <div class="flex gap-1.5"><input class="input !h-8 !text-xs flex-1" name="meet" placeholder="https://meet.google.com/abc-defg-hij" autocomplete="off"><button class="btn-primary btn-sm">Attach</button></div></form>`;
  return '';
}

export function bindMeet(node, e, after) {
  const f = node.querySelector('.meet-form');
  if (!f) return;
  f.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const link = f.querySelector('[name="meet"]').value.trim();
    try {
      await api('POST', `/api/personal/events/${e.event_no}/meet`, { meet_link: link });
      toast('Meet link attached - Google Calendar link updated', 'ok');
      if (after) after(); else load();
    } catch (ex) { toast(ex.message, 'error'); }
  });
}

function taskLine(t) {
  return `<div class="flex items-center gap-2 text-xs rounded-lg px-2 py-1.5 bg-violet-500/10"><span>${t.status === 'done' ? '✅' : '☐'}</span>
    <span class="flex-1 min-w-0 truncate ${t.status === 'done' ? 'line-through text-slate-400' : ''}">${esc(t.title)}</span><span class="text-slate-500">${esc(timeOnly(t.due_at))}</span>${refLink(t.ref)}</div>`;
}

function paintDay() {
  $('#cal-day-title', root).textContent = selected.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' });
  const box = $('#cal-day', root);
  const { ev, tk } = itemsOn(selected);
  box.innerHTML = '';
  if (!ev.length && !tk.length) box.innerHTML = '<p class="text-xs text-slate-500">Nothing planned.</p>';
  for (const e of ev) box.appendChild(eventCard(e));
  if (tk.length) box.insertAdjacentHTML('beforeend', `<div class="space-y-1">${tk.map(taskLine).join('')}</div>`);
  const add = document.createElement('button');
  add.className = 'btn-ghost btn-sm w-full';
  add.textContent = '+ Event on this day';
  add.addEventListener('click', () => newEvent(selected));
  box.appendChild(add);
}

function paintAgenda() {
  const box = $('#cal-agenda', root);
  const items = [...agenda.events.map((e) => ({ at: e.starts_at, e })), ...agenda.tasks.filter((t) => t.status !== 'done').map((t) => ({ at: t.due_at, t }))]
    .sort((a, b) => a.at.localeCompare(b.at));
  box.innerHTML = items.length ? '' : '<p class="text-xs text-slate-500">Nothing in the next two weeks.</p>';
  for (const it of items) {
    if (it.e) box.appendChild(eventCard(it.e));
    else box.insertAdjacentHTML('beforeend', taskLine(it.t));
  }
}

async function focusEvent(no) {
  const all = [...data.events, ...agenda.events];
  const e = all.find((x) => x.event_no === no);
  if (e) { const d = new Date(e.starts_at); month = new Date(d.getFullYear(), d.getMonth(), 1); selected = new Date(d.getFullYear(), d.getMonth(), d.getDate()); await load(); }
  flash(root && root.querySelector(`#cal-day [data-event-no="${no}"]`));
}

async function newEvent(day) {
  const d = day || selected || new Date();
  const form = formNode([
    { name: 'title', label: 'Title', full: true, placeholder: 'e.g. Call with Priya' },
    { name: 'date', label: 'Date', type: 'date', value: ymd(d) },
    { name: 'start', label: 'Start', type: 'time', value: '10:00' },
    { name: 'end', label: 'End', type: 'time', value: '11:00' },
    { name: 'location', label: 'Location', placeholder: 'optional' },
    { name: 'meet_link', label: 'Google Meet link', placeholder: 'https://meet.google.com/...', full: true },
    { name: 'wants_meet', label: 'I want a Google Meet (I\'ll paste the link later)', type: 'checkbox' },
    { name: 'description', label: 'Notes', type: 'textarea', rows: 3 },
  ]);
  const ok = await modal({ title: 'New event', body: form, actions: [{ label: 'Cancel', value: false }, { label: 'Create', kind: 'primary', value: true }] });
  if (!ok) return;
  const v = formValues(form);
  if (!v.title.trim()) { toast('A title is required', 'warn'); return; }
  const s = new Date(`${v.date}T${v.start || '09:00'}`);
  const e = new Date(`${v.date}T${v.end || v.start || '10:00'}`);
  try {
    const r = await api('POST', '/api/personal/events', { title: v.title.trim(), starts_at: s.toISOString(), ends_at: e > s ? e.toISOString() : null,
      location: v.location, meet_link: v.meet_link.trim(), wants_meet: !!v.wants_meet, description: v.description });
    toast(`${r.event.ref} created`, 'ok');
    selected = new Date(s.getFullYear(), s.getMonth(), s.getDate());
    month = new Date(s.getFullYear(), s.getMonth(), 1);
    load();
  } catch (ex) { toast(ex.message, 'error'); }
}
