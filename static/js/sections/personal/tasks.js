// Tasks tab - reminders (cancellable until 3 min before), location where the
// task was created, and the same-name processing queue.
import { $, $$, api, busy, cancelJob, confirmModal, debounce, getLocation, modal, state, toast } from '../../core.js';
import { esc, flash, formNode, formValues, fromLocalInput, refLink, STATUS_BADGE, toLocalInput } from './util.js';

let root = null;
let C = null;
let filter = 'open';
const unsubs = [];
let pendingFocus = null;

export function relFuture(iso) {
  if (!iso) return '';
  const s = (new Date(iso).getTime() - Date.now()) / 1000;
  const abs = Math.abs(s);
  const txt = abs < 60 ? `${Math.round(abs)}s` : abs < 3600 ? `${Math.round(abs / 60)}m` : abs < 86400 ? `${Math.round(abs / 3600)}h` : `${Math.round(abs / 86400)}d`;
  return s >= 0 ? `in ${txt}` : `${txt} ago`;
}

export async function mount(el, ctx) {
  root = el; C = ctx;
  el.innerHTML = `<div class="h-full overflow-y-auto scroll-thin p-5 md:p-8">
    <div class="flex flex-wrap items-center gap-3">
      <div><h2 class="text-xl font-semibold">Tasks</h2>
        <p class="text-sm text-slate-500">Day-to-day tasks with reminders. A task with the same name as an active one waits in the processing queue until the earlier one is done.</p></div>
      <span class="flex-1"></span>
      <div class="seg" id="t-filter">${['open', 'active', 'queued', 'done', 'all'].map((f) => `<button class="seg-item ${f === filter ? 'active' : ''}" data-f="${f}">${f[0].toUpperCase() + f.slice(1)}</button>`).join('')}</div>
    </div>
    <form id="t-new" class="mt-5 card grid md:grid-cols-[1fr_230px_auto] gap-2 items-end" novalidate>
      <div><label class="label" for="t-title">New task</label><input class="input" id="t-title" name="title" required maxlength="200" placeholder="e.g. Eat dinner"></div>
      <div><label class="label" for="t-due">Due (optional)</label><input class="input" id="t-due" type="datetime-local" name="due"></div>
      <button class="btn-primary" id="t-add">Add task</button>
      <div class="md:col-span-3 flex flex-wrap items-center gap-3 text-xs text-slate-500">
        <label class="flex items-center gap-1.5"><input type="checkbox" name="useloc" ${state.me?.providers?.share_location ? 'checked' : ''}> Save where I am</label>
        <input class="input !h-8 !w-56 !text-xs" name="location_name" maxlength="200" placeholder="or type a place">
        <input class="input !h-8 flex-1 min-w-[180px] !text-xs" name="description" maxlength="2000" placeholder="details (optional)">
      </div>
    </form>
    <div class="mt-5 grid lg:grid-cols-[1fr_330px] gap-5 items-start">
      <div id="t-list" class="space-y-2"><p class="text-sm text-slate-500">Loading…</p></div>
      <aside class="space-y-4 lg:sticky lg:top-0">
        <div class="card"><div class="flex items-center gap-2"><h3 class="font-semibold text-sm">Processing queue</h3><span class="chip" id="t-qcount">0</span></div>
          <p class="text-[11px] text-slate-500 mt-0.5">Same-name tasks wait here (for as long as the app runs) and activate one by one.</p>
          <div id="t-queue" class="mt-3 space-y-3 text-sm"></div></div>
        <div class="card text-[11px] text-slate-500 space-y-1.5">
          <p>⏰ Reminders run as background tasks - cancel them here or in Background tasks up to <b>3 minutes</b> before they are due.</p>
          <p>📍 Tasks remember where they were created (when location sharing is on).</p>
          <p>💬 Ask in chat: <i>"remind me to eat dinner at 9pm"</i>, <i>"mark eat dinner as done"</i>.</p></div>
      </aside>
    </div>
  </div>`;
  for (const b of $$('#t-filter [data-f]', el)) b.addEventListener('click', () => { filter = b.dataset.f; $$('#t-filter [data-f]', el).forEach((x) => x.classList.toggle('active', x === b)); load(); });
  $('#t-new', el).addEventListener('submit', add);
  const reload = debounce(load, 300);
  unsubs.push(ctx.on('task:changed', reload));
  unsubs.push(ctx.on('job', (j) => { if (j.kind === 'reminder') reload(); }));
  unsubs.push(ctx.on('ref:focus', (r) => { if (r.type === 'task') { pendingFocus = r.no; if (filter !== 'all') { filter = 'all'; $$('#t-filter [data-f]', el).forEach((x) => x.classList.toggle('active', x.dataset.f === 'all')); } load(); } }));
  await load();
}

export function unmount() { while (unsubs.length) unsubs.pop()(); root = null; }

async function add(e) {
  e.preventDefault();
  const f = formValues(e.target);
  if (!f.title.trim()) { toast('Give the task a title', 'warn'); return; }
  const btn = $('#t-add', root);
  busy(btn, true);
  try {
    const body = { title: f.title.trim(), due_at: fromLocalInput(f.due), description: f.description || '', location_name: f.location_name || null };
    if (f.useloc) { const loc = await getLocation(); if (loc) body.location = loc; }
    const r = await api('POST', '/api/personal/tasks', body);
    e.target.reset();
    toast(r.queued_behind ? `${r.task.ref} queued behind task#${r.queued_behind} (same name)` : `${r.task.ref} created`, r.queued_behind ? 'warn' : 'ok', 5000);
    await load();
  } catch (ex) { toast(ex.message, 'error'); } finally { busy(btn, false); }
}

async function load() {
  if (!root) return;
  let data;
  try { data = await api('GET', `/api/personal/tasks?status=${filter}`); } catch (e) { $('#t-list', root).innerHTML = `<p class="text-rose-600 text-sm">${esc(e.message)}</p>`; return; }
  const list = $('#t-list', root);
  if (!list) return;
  list.innerHTML = data.tasks.length ? '' : `<div class="card text-center text-sm text-slate-500 py-10">No ${esc(filter)} tasks.</div>`;
  for (const t of data.tasks) list.appendChild(row(t));
  paintQueue(data.queue);
  if (pendingFocus) { flash(list.querySelector(`[data-task-no="${pendingFocus}"]`)); pendingFocus = null; }
}

function reminderLine(t) {
  if (t.status !== 'active') return '';
  if (t.reminder && ['scheduled', 'running'].includes(t.reminder.status)) {
    const until = t.reminder.cancel_deadline ? new Date(t.reminder.cancel_deadline).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }) : '';
    return `<div class="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-sky-700 dark:text-sky-300">🔔 Reminder scheduled
      ${t.reminder.can_cancel ? `<span class="text-slate-500">· cancellable until ${esc(until)}</span><button class="btn-ghost btn-sm !h-6" data-act="cancel-reminder" data-rid="${esc(t.reminder.request_id)}">Cancel reminder</button>`
        : `<span class="text-amber-600">· ${esc(t.reminder.cancel_reason || 'can no longer be cancelled')}</span>`}</div>`;
  }
  if (t.reminder_cancelled) return '<div class="mt-1.5 text-xs text-slate-500">🔕 Reminder cancelled</div>';
  if (t.reminded_at) return `<div class="mt-1.5 text-xs text-slate-500">🔔 Reminded ${esc(relFuture(t.reminded_at))}</div>`;
  return '';
}

function row(t) {
  const div = document.createElement('div');
  div.className = 'card !p-3 flex gap-3 items-start transition';
  div.dataset.taskNo = t.task_no;
  const done = t.status === 'done';
  div.innerHTML = `
    <button class="mt-0.5 w-6 h-6 shrink-0 rounded-full border-2 flex items-center justify-center text-xs transition ${done ? 'bg-emerald-500 border-emerald-500 text-white' : 'border-slate-400/60 hover:border-emerald-500 hover:bg-emerald-500/10'} disabled:opacity-40"
      data-act="${done ? 'reopen' : 'complete'}" title="${done ? 'Reopen' : t.status === 'queued' ? 'Queued - finish the earlier task first, or complete it anyway' : 'Mark done'}" aria-label="${done ? 'Reopen task' : 'Mark task done'}">${done ? '✓' : ''}</button>
    <div class="min-w-0 flex-1">
      <div class="flex flex-wrap items-center gap-2"><span class="font-medium break-words ${done ? 'line-through text-slate-400' : ''}">${esc(t.title)}</span>
        <span class="${STATUS_BADGE[t.status] || 'badge-gray'}">${esc(t.status)}</span>
        ${t.stale ? '<span class="badge-amber" title="The memory of this section was deleted - the task is kept but marked stale">stale</span>' : ''}
        ${t.overdue ? '<span class="badge-red">overdue</span>' : ''}</div>
      <div class="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-slate-500">
        <span>${refLink(t.ref)}</span>
        ${t.due_local ? `<span>⏰ ${esc(t.due_local)} · ${esc(relFuture(t.due_at))}</span>` : ''}
        ${t.location_name ? `<span title="Where the task was created">📍 ${esc(t.location_name)}</span>` : ''}
        ${t.source_ref ? `<span>from ${refLink(t.source_ref)}</span>` : ''}
        ${t.section !== 'personal' ? `<span class="chip !h-4 !text-[10px]">${esc(t.section)}</span>` : ''}
        ${done && t.completed_at ? `<span>done ${esc(relFuture(t.completed_at))}</span>` : ''}
      </div>
      ${t.status === 'queued' ? `<div class="mt-1.5 text-xs text-violet-600 dark:text-violet-300">⏳ Waiting behind ${t.queued_behind_no ? refLink('task#' + t.queued_behind_no) : 'an earlier task'}${t.queue_position ? ` · position ${t.queue_position.position} of ${t.queue_position.length}` : ''}</div>` : ''}
      ${reminderLine(t)}
      ${t.description ? `<div class="mt-1 text-sm text-slate-600 dark:text-slate-300 break-words">${esc(t.description)}</div>` : ''}
    </div>
    <div class="flex flex-col gap-1 shrink-0">
      <button class="btn-plain btn-sm" data-act="edit">Edit</button>
      <button class="btn-plain btn-sm text-rose-600" data-act="delete">Delete</button>
    </div>`;
  div.addEventListener('click', (e) => onAction(e, t));
  return div;
}

async function onAction(e, t) {
  const b = e.target.closest('[data-act]');
  if (!b) return;
  const act = b.dataset.act;
  try {
    if (act === 'complete') {
      b.disabled = true;
      const r = await api('POST', `/api/personal/tasks/${t.task_no}/complete`);
      toast(r.activated ? `${t.ref} done · ${r.activated.ref} is now active` : `${t.ref} done`, 'ok');
    } else if (act === 'reopen') {
      await api('POST', `/api/personal/tasks/${t.task_no}/reopen`);
    } else if (act === 'delete') {
      if (!(await confirmModal('Delete task?', `${t.ref} "${t.title}" will be deleted.`, { danger: true, confirmText: 'Delete' }))) return;
      const r = await api('DELETE', `/api/personal/tasks/${t.task_no}`);
      toast(r.activated ? `Deleted · task#${r.activated} is now active` : 'Task deleted', 'ok');
    } else if (act === 'cancel-reminder') {
      b.disabled = true;
      const ack = await cancelJob(b.dataset.rid);
      toast(ack.ok ? 'Reminder cancelled' : `Not cancelled: ${ack.message}`, ack.ok ? 'ok' : 'warn', 6000);
    } else if (act === 'edit') {
      await edit(t);
    }
  } catch (ex) { toast(ex.message, 'error'); }
  load();
}

async function edit(t) {
  const form = formNode([
    { name: 'title', label: 'Title', value: t.title, full: true },
    { name: 'due', label: 'Due', type: 'datetime-local', value: toLocalInput(t.due_at) },
    { name: 'location_name', label: 'Location', value: t.location_name },
    { name: 'description', label: 'Details', type: 'textarea', value: t.description, rows: 3 },
  ]);
  const ok = await modal({ title: `Edit ${t.ref}`, body: form, actions: [{ label: 'Cancel', value: false }, { label: 'Save', kind: 'primary', value: true }] });
  if (!ok) return;
  const v = formValues(form);
  await api('PATCH', `/api/personal/tasks/${t.task_no}`, { title: v.title, description: v.description, location_name: v.location_name, due_at: v.due ? fromLocalInput(v.due) : '' });
  toast(`${t.ref} updated`, 'ok');
}

function paintQueue(queue) {
  const box = $('#t-queue', root);
  if (!box) return;
  const waiting = queue.reduce((n, q) => n + q.waiting.length, 0);
  $('#t-qcount', root).textContent = waiting;
  if (!queue.length) { box.innerHTML = '<p class="text-xs text-slate-500">Nothing waiting.</p>'; return; }
  box.innerHTML = queue.map((q) => `<div class="rounded-xl glass-soft p-2.5">
      <div class="text-xs font-semibold text-slate-700 dark:text-slate-200">“${esc(q.name_key)}”</div>
      ${q.active ? `<div class="mt-1 text-xs"><span class="badge-blue">active</span> ${refLink(q.active.ref)} ${esc(q.active.due_local || '')}</div>` : '<div class="mt-1 text-xs text-amber-600">no active task - activating next…</div>'}
      <ol class="mt-1 space-y-0.5 text-xs list-decimal pl-5">${q.waiting.map((w) => `<li>${refLink(w.ref)} ${esc(w.title)} ${w.due_local ? '· ' + esc(w.due_local) : ''}</li>`).join('')}</ol></div>`).join('');
}
