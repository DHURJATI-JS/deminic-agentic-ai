// Business section: clients, payment reminders, credit/debit statements, sent inbox,
// daily world & market news, what-if simulations and the AI business summary.
// Strict CSP: no inline handlers/styles in markup - listeners are attached in JS,
// dynamic widths are set through the CSSOM, every text goes through esc().
import { $, $$, api, bizvars, busy, confirmModal, el, esc, fmtDate, fmtRel, modal, on, toast } from '../core.js';
import { cardShell } from '../cards.js';

let C = null;
const subs = [];
let pendingFocus = null;

const sub = (off) => subs.push(off);
const unsubAll = () => { while (subs.length) subs.pop()(); };
const ref = (r) => (r ? `<a class="ref-link" data-ref="${esc(r)}">${esc(r)}</a>` : '');
const HEALTH = { healthy: 'badge-green', stable: 'badge-blue', at_risk: 'badge-amber', critical: 'badge-red', unknown: 'badge-gray' };
const SENT = { bullish: 'badge-green', neutral: 'badge-gray', bearish: 'badge-red', mixed: 'badge-amber' };
const STATUS = { pending: 'badge-blue', done: 'badge-green', cancelled: 'badge-gray', draft: 'badge-amber', sent: 'badge-green', failed: 'badge-red' };

function header(title, subtitle, actions = '') {
  return `<div class="flex flex-wrap items-end gap-3"><div class="min-w-0"><h2 class="text-xl font-semibold">${title}</h2>
    <p class="text-sm text-slate-500">${subtitle}</p></div><span class="flex-1"></span>${actions}</div>`;
}
function shell(inner) { return `<div class="h-full overflow-y-auto scroll-thin p-5 md:p-8 space-y-5">${inner}</div>`; }
function setWidths(root) { for (const b of $$('[data-w]', root)) b.style.width = `${Math.max(2, Math.min(100, Number(b.dataset.w) || 0))}%`; }

function focusRow(type, no) {
  const node = document.querySelector(`[data-row="${type}-${no}"]`);
  if (!node) { pendingFocus = { type, no }; return; }
  pendingFocus = null;
  node.scrollIntoView({ behavior: 'smooth', block: 'center' });
  node.classList.add('ring-2', 'ring-accent-500/60');
  setTimeout(() => node.classList.remove('ring-2', 'ring-accent-500/60'), 2600);
}
function applyPendingFocus() { if (pendingFocus) focusRow(pendingFocus.type, pendingFocus.no); }

function toLocalInput(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

// horizontal +/- bars (simulation impacts, statement totals)
function barsSVG(items, { unit = '%', width = 360 } = {}) {
  const rowH = 26;
  const max = Math.max(1, ...items.map((i) => Math.abs(Number(i.value) || 0)));
  const mid = width / 2;
  const rows = items.map((it, i) => {
    const v = Number(it.value) || 0;
    const w = Math.max(1, (Math.abs(v) / max) * (mid - 70));
    const x = v >= 0 ? mid : mid - w;
    const y = i * rowH + 4;
    return `<text x="4" y="${y + 13}" class="fill-slate-500 text-[10px]">${esc(it.label)}</text>
      <rect x="${x}" y="${y + 2}" width="${w}" height="14" rx="4" class="${v >= 0 ? 'fill-emerald-500' : 'fill-rose-500'}"></rect>
      <text x="${v >= 0 ? x + w + 4 : x - 4}" y="${y + 13}" text-anchor="${v >= 0 ? 'start' : 'end'}" class="fill-slate-600 dark:fill-slate-300 text-[10px] font-semibold">${v > 0 ? '+' : ''}${esc(String(Math.round(v * 10) / 10))}${unit}</text>`;
  }).join('');
  const h = items.length * rowH + 8;
  return `<svg viewBox="0 0 ${width} ${h}" class="w-full max-w-md" role="img" aria-label="impact chart">
    <line x1="${mid}" y1="0" x2="${mid}" y2="${h}" class="stroke-slate-300 dark:stroke-slate-600" stroke-dasharray="3 3"></line>${rows}</svg>`;
}

// ============================================================ Clients
async function mountClients(root) {
  root.innerHTML = shell(`${header('Clients', 'Every client remembers the chat it was added from and why. Removing a client removes its payment reminders too.',
    '<button class="btn-primary" id="b-add-client">Add client</button>')}
    <div id="b-overview" class="grid grid-cols-2 md:grid-cols-4 gap-3"></div>
    <input id="b-client-q" class="input !w-72" placeholder="Filter clients…" aria-label="Filter clients">
    <div id="b-clients" class="grid md:grid-cols-2 xl:grid-cols-3 gap-3"></div>`);
  let all = [];
  const paint = () => {
    const q = ($('#b-client-q', root).value || '').toLowerCase();
    const list = all.filter((c) => !q || `${c.name} ${c.company} ${c.email} ${c.reason_added}`.toLowerCase().includes(q));
    const box = $('#b-clients', root);
    if (!list.length) { box.innerHTML = `<p class="col-span-full text-center text-sm text-slate-500 py-12">${all.length ? 'No matches' : 'No clients yet - add one, or tell your AI "add Acme Corp as a client because..."'}</p>`; return; }
    box.innerHTML = list.map((c) => `<article class="card space-y-2" data-row="client-${c.client_no}">
      <div class="flex items-start gap-2"><div class="w-10 h-10 rounded-2xl bg-gradient-to-br from-amber-400 to-rose-500 text-white flex items-center justify-center font-semibold">${esc((c.name || '?').slice(0, 1).toUpperCase())}</div>
        <div class="min-w-0 flex-1"><div class="font-semibold truncate">${esc(c.name)}</div><div class="text-xs text-slate-500 truncate">${esc(c.company || '')}</div></div>
        ${ref(c.ref)}</div>
      <div class="text-xs text-slate-600 dark:text-slate-300 space-y-0.5">${c.email ? `<div>✉️ ${esc(c.email)}</div>` : ''}${c.phone ? `<div>☎️ ${esc(c.phone)}</div>` : ''}</div>
      ${c.reason_added ? `<div class="text-xs"><span class="text-slate-500">Why added:</span> ${esc(c.reason_added)}</div>` : ''}
      <div class="flex flex-wrap items-center gap-1.5 text-[11px] text-slate-500">${c.source ? `from ${ref(c.source)}` : 'added manually'} · ${esc(fmtRel(c.created_at))}
        ${c.pending_reminders ? `<span class="badge-amber">${c.pending_reminders} pending · ${esc(Object.values(c.pending_totals).join(', '))}</span>` : ''}</div>
      <div class="flex gap-1.5 pt-1"><button class="btn-ghost btn-sm" data-act="view" data-no="${c.client_no}">Details</button>
        <button class="btn-ghost btn-sm" data-act="edit" data-no="${c.client_no}">Edit</button>
        <button class="btn-plain btn-sm text-rose-600 ml-auto" data-act="del" data-no="${c.client_no}">Remove</button></div></article>`).join('');
    applyPendingFocus();
  };
  const load = async () => {
    const [cl, ov] = await Promise.all([api('GET', '/api/business/clients'), api('GET', '/api/business/overview')]);
    all = cl.clients;
    const nets = Object.values(ov.totals).map((t) => t.net_fmt).join(' · ') || '0';
    $('#b-overview', root).innerHTML = [
      ['Clients', ov.clients, 'total'], ['Pending payments', ov.pending_reminders, Object.values(ov.pending_totals).join(', ') || '-'],
      ['Overdue', ov.overdue, 'reminders past due'], ['Net (statements)', nets, `${ov.drafts} draft e-mail(s)`],
    ].map(([l, v, s]) => `<div class="card !p-3"><div class="text-[11px] uppercase tracking-wide text-slate-500">${esc(l)}</div><div class="text-xl font-semibold truncate">${esc(String(v))}</div><div class="text-[11px] text-slate-500 truncate">${esc(s)}</div></div>`).join('');
    paint();
  };
  $('#b-client-q', root).addEventListener('input', paint);
  $('#b-add-client', root).addEventListener('click', () => clientForm(null, load));
  $('#b-clients', root).addEventListener('click', async (e) => {
    const b = e.target.closest('[data-act]');
    if (!b) return;
    const no = Number(b.dataset.no);
    const c = all.find((x) => x.client_no === no);
    if (b.dataset.act === 'view') return clientDetails(no);
    if (b.dataset.act === 'edit') return clientForm(c, load);
    if (b.dataset.act === 'del') {
      if (!(await confirmModal(`Remove ${c.name}?`, `client#${no} and ALL of its payment reminders (${c.reminders}) will be deleted; scheduled reminder tasks are cancelled.`, { danger: true, confirmText: 'Remove client' }))) return;
      const r = await api('DELETE', `/api/business/clients/${no}`);
      toast(`Removed ${r.name} · ${r.reminders_removed} reminder(s) removed`, 'ok');
      load();
    }
  });
  sub(on('client:changed', () => load()));
  await load();
}

async function clientForm(c, after) {
  const f = el(`<form class="grid gap-3">
    <div><label class="label">Name</label><input class="input" name="name" required maxlength="160" value="${esc(c?.name || '')}"></div>
    <div class="grid grid-cols-2 gap-3"><div><label class="label">Company</label><input class="input" name="company" maxlength="160" value="${esc(c?.company || '')}"></div>
    <div><label class="label">Phone</label><input class="input" name="phone" maxlength="40" value="${esc(c?.phone || '')}"></div></div>
    <div><label class="label">E-mail</label><input class="input" name="email" type="email" maxlength="254" value="${esc(c?.email || '')}"></div>
    <div><label class="label">Why is this client added?</label><textarea class="textarea" name="reason" rows="2" maxlength="2000">${esc(c?.reason_added || '')}</textarea></div>
    <div><label class="label">Notes</label><textarea class="textarea" name="notes" rows="2" maxlength="4000">${esc(c?.notes || '')}</textarea></div></form>`);
  const ok = await modal({ title: c ? `Edit client#${c.client_no}` : 'Add client', body: f, actions: [{ label: 'Cancel', value: false }, { label: 'Save', kind: 'primary', value: true }] });
  if (!ok) return;
  const d = Object.fromEntries(new FormData(f).entries());
  try {
    if (c) await api('PATCH', `/api/business/clients/${c.client_no}`, { name: d.name, company: d.company, phone: d.phone, email: d.email, notes: d.notes, reason_added: d.reason });
    else await api('POST', '/api/business/clients', d);
    toast('Client saved', 'ok');
    after && after();
  } catch (e) { toast(e.message, 'error'); }
}

async function clientDetails(no) {
  const r = await api('GET', `/api/business/clients/${no}`);
  const c = r.client;
  const body = el(`<div class="space-y-3"><div class="text-sm">${esc(c.company || '')} ${c.email ? '· ' + esc(c.email) : ''} ${c.phone ? '· ' + esc(c.phone) : ''}</div>
    <div class="card !p-3 text-sm"><b>Why added:</b> ${esc(c.reason_added || '-')}<div class="text-xs text-slate-500 mt-1">${c.source ? 'from ' + ref(c.source) : 'added manually'} · ${esc(fmtDate(c.created_at))}</div></div>
    ${c.notes ? `<div class="text-sm whitespace-pre-wrap">${esc(c.notes)}</div>` : ''}
    <h4 class="font-semibold text-sm">Payment reminders (${r.reminders.length})</h4>
    <div class="space-y-1.5">${r.reminders.map((x) => `<div class="flex items-center gap-2 text-sm">${ref(x.ref)}<span class="flex-1 truncate">${esc(x.title)}</span><b>${esc(x.amount_fmt)}</b>
      <span class="text-xs text-slate-500">${esc(x.due_local || '')}</span><span class="${STATUS[x.status] || 'badge-gray'}">${esc(x.overdue ? 'overdue' : x.status)}</span></div>`).join('') || '<p class="text-xs text-slate-500">None</p>'}</div></div>`);
  modal({ title: `client#${c.client_no} · ${c.name}`, body, wide: true, actions: [{ label: 'Close', value: true }] });
}

// ============================================================ Reminders
async function mountReminders(root) {
  root.innerHTML = shell(`${header('Payment reminders', 'Scheduled as background tasks - cancellable until 3 minutes before they are due. Removing a client removes its reminders.',
    '<div class="seg" id="b-rem-filter">' + ['pending', 'done', 'cancelled', 'all'].map((s) => `<button class="seg-item ${s === 'pending' ? 'active' : ''}" data-f="${s}">${s}</button>`).join('') + '</div><button class="btn-primary" id="b-add-rem">Add reminder</button>')}
    <div class="panel !rounded-2xl overflow-x-auto"><table class="table-glass"><thead><tr><th>Ref</th><th>Reminder</th><th>Client</th><th>Amount</th><th>Due</th><th>Status</th><th></th></tr></thead><tbody id="b-rem-rows"></tbody></table></div>`);
  let filter = 'pending';
  let rows = [];
  const load = async () => {
    rows = (await api('GET', `/api/business/reminders?status=${filter}`)).reminders;
    const tb = $('#b-rem-rows', root);
    tb.innerHTML = rows.length ? rows.map((r) => `<tr data-row="reminder-${r.reminder_no}">
      <td>${ref(r.ref)}</td><td><div class="font-medium">${esc(r.title)}</div><div class="text-[11px] text-slate-500">${r.source ? 'from ' + ref(r.source) : 'manual'}</div></td>
      <td>${r.client_ref ? `${ref(r.client_ref)} <span class="text-xs">${esc(r.client_name)}</span>` : '<span class="text-xs text-slate-500">-</span>'}</td>
      <td class="font-semibold whitespace-nowrap">${esc(r.amount_fmt || '-')}</td>
      <td class="whitespace-nowrap text-xs">${esc(r.due_local || 'no date')}${r.overdue ? ' <span class="badge-red">overdue</span>' : ''}${r.job && r.status === 'pending' ? `<div class="text-[10px] text-slate-500">task: ${esc(r.job.status)}</div>` : ''}</td>
      <td><span class="${STATUS[r.status] || 'badge-gray'}">${esc(r.status)}</span>${r.stale ? ' <span class="badge-amber" title="Memory of this section was wiped">stale</span>' : ''}</td>
      <td class="whitespace-nowrap text-right">${r.status === 'pending' ? `<button class="btn-ghost btn-sm" data-act="done" data-no="${r.reminder_no}">Paid</button>
        <button class="btn-ghost btn-sm" data-act="edit" data-no="${r.reminder_no}" ${r.can_cancel ? '' : 'disabled'}>Edit</button>
        <button class="btn-plain btn-sm text-rose-600" data-act="cancel" data-no="${r.reminder_no}" ${r.can_cancel ? '' : `disabled title="${esc(r.cancel_reason)}"`}>Cancel</button>` : `<button class="btn-plain btn-sm" data-act="del" data-no="${r.reminder_no}">Delete</button>`}</td></tr>`).join('')
      : `<tr><td colspan="7" class="text-center text-sm text-slate-500 py-10">No ${filter === 'all' ? '' : filter} reminders</td></tr>`;
    applyPendingFocus();
  };
  $('#b-rem-filter', root).addEventListener('click', (e) => {
    const b = e.target.closest('[data-f]'); if (!b) return;
    filter = b.dataset.f; $$('[data-f]', root).forEach((x) => x.classList.toggle('active', x === b)); load();
  });
  $('#b-add-rem', root).addEventListener('click', () => reminderForm(null, load));
  $('#b-rem-rows', root).addEventListener('click', async (e) => {
    const b = e.target.closest('[data-act]'); if (!b || b.disabled) return;
    const no = Number(b.dataset.no);
    try {
      if (b.dataset.act === 'done') { await api('POST', `/api/business/reminders/${no}/done`); toast(`reminder#${no} marked paid`, 'ok'); }
      if (b.dataset.act === 'cancel') { if (!(await confirmModal('Cancel reminder?', `reminder#${no} and its scheduled task will be cancelled.`, { confirmText: 'Cancel reminder' }))) return; await api('POST', `/api/business/reminders/${no}/cancel`); toast('Reminder cancelled', 'ok'); }
      if (b.dataset.act === 'del') { await api('DELETE', `/api/business/reminders/${no}`); toast('Reminder deleted', 'ok'); }
      if (b.dataset.act === 'edit') return reminderForm(rows.find((x) => x.reminder_no === no), load);
      load();
    } catch (ex) { toast(ex.message, 'warn', 6000); load(); }
  });
  sub(on('client:changed', () => load()));
  sub(on('job', (j) => { if (j.kind === 'reminder' && j.meta?.section === 'business') load(); }));
  await load();
}

async function reminderForm(r, after) {
  let clients = [];
  try { clients = (await api('GET', '/api/business/clients')).clients; } catch { clients = []; }
  const f = el(`<form class="grid gap-3">
    ${r ? '' : `<div><label class="label">Client</label><select class="select" name="client_no"><option value="">- none / new -</option>${clients.map((c) => `<option value="${c.client_no}">${esc(c.name)} (client#${c.client_no})</option>`).join('')}</select>
      <input class="input mt-2" name="client_name" placeholder="…or a new client name" maxlength="160"></div>`}
    <div><label class="label">What is due?</label><input class="input" name="title" maxlength="200" value="${esc(r?.title || '')}" placeholder="Invoice #42"></div>
    <div class="grid grid-cols-3 gap-3"><div class="col-span-2"><label class="label">Amount</label><input class="input" name="amount" value="${esc(r?.amount || '')}" placeholder="2000"></div>
      <div><label class="label">Currency</label><input class="input" name="currency" maxlength="8" value="${esc(r?.currency || '')}" placeholder="USD"></div></div>
    <div><label class="label">Due</label><input class="input" name="due_at" type="datetime-local" value="${esc(toLocalInput(r?.due_at))}"></div>
    <p class="hint">Reminders become background tasks and can be cancelled until 3 minutes before they are due.</p></form>`);
  const ok = await modal({ title: r ? `Edit reminder#${r.reminder_no}` : 'Add payment reminder', body: f, actions: [{ label: 'Cancel', value: false }, { label: 'Save', kind: 'primary', value: true }] });
  if (!ok) return;
  const d = Object.fromEntries(new FormData(f).entries());
  try {
    if (r) await api('PATCH', `/api/business/reminders/${r.reminder_no}`, { title: d.title, amount: d.amount, currency: d.currency, due_at: d.due_at || '' });
    else await api('POST', '/api/business/reminders', { client_no: d.client_no ? Number(d.client_no) : null, client_name: d.client_name || '', title: d.title, amount: d.amount, currency: d.currency, due_at: d.due_at || null });
    toast('Reminder saved', 'ok');
    after && after();
  } catch (e) { toast(e.message, 'error', 6000); }
}

// ============================================================ Statements
async function mountStatements(root) {
  root.innerHTML = shell(`${header('Credit & debit statements', 'Money in and out, each linked to the chat it came from. Amounts are encrypted at rest.',
    '<button class="btn-primary" id="b-add-txn">Add entry</button>')}
    <div id="b-totals" class="grid sm:grid-cols-2 xl:grid-cols-3 gap-3"></div>
    <div class="flex flex-wrap gap-2 items-center"><div class="seg" id="b-kind">${['all', 'credit', 'debit'].map((k) => `<button class="seg-item ${k === 'all' ? 'active' : ''}" data-k="${k}">${k}</button>`).join('')}</div>
      <select id="b-cur" class="select !w-32 !h-9"><option value="">All currencies</option></select>
      <input id="b-txn-q" class="input !w-60 !h-9" placeholder="Search counterparty / note"></div>
    <div class="panel !rounded-2xl overflow-x-auto"><table class="table-glass"><thead><tr><th>Ref</th><th>Date</th><th>Type</th><th>Counterparty</th><th>Description</th><th class="text-right">Amount</th><th>Source</th><th></th></tr></thead><tbody id="b-txn-rows"></tbody></table></div>`);
  let kind = ''; let cur = ''; let q = '';
  const load = async () => {
    const r = await api('GET', `/api/business/transactions?kind=${kind}&currency=${encodeURIComponent(cur)}&q=${encodeURIComponent(q)}`);
    const sel = $('#b-cur', root);
    if (sel.options.length - 1 !== r.currencies.length) sel.innerHTML = '<option value="">All currencies</option>' + r.currencies.map((c) => `<option ${c === cur ? 'selected' : ''}>${esc(c)}</option>`).join('');
    const tots = Object.values(r.all_totals);
    $('#b-totals', root).innerHTML = tots.length ? tots.map((t) => `<div class="card"><div class="flex items-center gap-2"><b>${esc(t.currency || 'No currency')}</b><span class="text-xs text-slate-500">${t.count} entries</span>
      <span class="ml-auto ${Number(t.net) >= 0 ? 'badge-green' : 'badge-red'}">net ${esc(t.net_fmt)}</span></div>
      <div class="mt-2">${barsSVG([{ label: 'credits', value: Number(t.credits) }, { label: 'debits', value: -Number(t.debits) }], { unit: '' })}</div></div>`).join('')
      : '<p class="text-sm text-slate-500">No entries yet - tell your AI "we received 50,000 from Zenith" or add one.</p>';
    $('#b-txn-rows', root).innerHTML = r.transactions.length ? r.transactions.map((t) => `<tr data-row="txn-${t.txn_no}">
      <td>${ref(t.ref)}</td><td class="text-xs whitespace-nowrap">${esc(t.occurred_on || '')}</td>
      <td><span class="${t.kind === 'credit' ? 'badge-green' : 'badge-red'}">${t.kind === 'credit' ? 'credit ↓' : 'debit ↑'}</span></td>
      <td>${esc(t.counterparty || '-')}</td><td class="text-xs max-w-xs">${esc(t.description || '')}</td>
      <td class="text-right font-semibold whitespace-nowrap ${t.kind === 'credit' ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}">${t.kind === 'credit' ? '+' : '−'}${esc(t.amount_fmt)}</td>
      <td>${t.source ? ref(t.source) : '<span class="text-xs text-slate-500">manual</span>'}</td>
      <td><button class="btn-plain btn-sm text-rose-600" data-del="${t.txn_no}">Delete</button></td></tr>`).join('')
      : '<tr><td colspan="8" class="text-center text-sm text-slate-500 py-10">No matching entries</td></tr>';
    applyPendingFocus();
  };
  $('#b-kind', root).addEventListener('click', (e) => { const b = e.target.closest('[data-k]'); if (!b) return; kind = b.dataset.k === 'all' ? '' : b.dataset.k; $$('[data-k]', root).forEach((x) => x.classList.toggle('active', x === b)); load(); });
  $('#b-cur', root).addEventListener('change', (e) => { cur = e.target.value; load(); });
  let t;
  $('#b-txn-q', root).addEventListener('input', (e) => { clearTimeout(t); t = setTimeout(() => { q = e.target.value.trim(); load(); }, 300); });
  $('#b-txn-rows', root).addEventListener('click', async (e) => {
    const b = e.target.closest('[data-del]'); if (!b) return;
    if (!(await confirmModal('Delete entry?', `txn#${b.dataset.del} will be removed from the statements.`, { danger: true, confirmText: 'Delete' }))) return;
    await api('DELETE', `/api/business/transactions/${b.dataset.del}`); load();
  });
  $('#b-add-txn', root).addEventListener('click', async () => {
    const f = el(`<form class="grid gap-3"><div class="seg w-max"><label class="seg-item"><input type="radio" name="kind" value="credit" checked class="mr-1">Credit (money in)</label><label class="seg-item"><input type="radio" name="kind" value="debit" class="mr-1">Debit (money out)</label></div>
      <div class="grid grid-cols-3 gap-3"><div class="col-span-2"><label class="label">Amount</label><input class="input" name="amount" required placeholder="50000"></div><div><label class="label">Currency</label><input class="input" name="currency" maxlength="8" placeholder="INR"></div></div>
      <div><label class="label">Counterparty</label><input class="input" name="counterparty" maxlength="160"></div>
      <div><label class="label">Description</label><input class="input" name="description" maxlength="2000"></div>
      <div><label class="label">Date</label><input class="input" name="occurred_on" type="date"></div></form>`);
    const ok = await modal({ title: 'Add statement entry', body: f, actions: [{ label: 'Cancel', value: false }, { label: 'Save', kind: 'primary', value: true }] });
    if (!ok) return;
    try { await api('POST', '/api/business/transactions', Object.fromEntries(new FormData(f).entries())); toast('Entry saved', 'ok'); load(); } catch (ex) { toast(ex.message, 'error'); }
  });
  sub(on('txn:changed', () => load()));
  await load();
}

// ============================================================ Sent inbox
async function mountInbox(root) {
  root.innerHTML = `<div class="h-full flex flex-col md:flex-row min-h-0">
    <div class="md:w-[380px] shrink-0 border-b md:border-b-0 md:border-r border-slate-900/[0.06] dark:border-white/[0.07] flex flex-col min-h-0 max-h-[45%] md:max-h-none">
      <div class="p-4 space-y-3"><div class="flex items-center gap-2"><h2 class="text-lg font-semibold">Sent inbox</h2><span class="flex-1"></span><button class="btn-primary btn-sm" id="b-compose">Compose</button></div>
        <div class="seg" id="b-mail-filter">${['all', 'draft', 'sent', 'failed'].map((s) => `<button class="seg-item ${s === 'all' ? 'active' : ''}" data-s="${s}">${s}</button>`).join('')}</div>
        <p id="b-smtp-hint" class="hidden text-[11px] text-amber-600">SMTP is not configured - drafts can't be sent until you add it in Settings.</p></div>
      <div id="b-mail-list" class="flex-1 overflow-y-auto scroll-thin px-2 pb-3 space-y-1"></div></div>
    <div id="b-reader" class="flex-1 min-w-0 overflow-y-auto scroll-thin p-5 md:p-8"><p class="text-sm text-slate-500 text-center pt-16">Select an e-mail. Cold sales e-mails drafted by your AI land here with a reference to their chat.</p></div></div>`;
  let status = ''; let mails = []; let openNo = null;
  const loadList = async () => {
    const r = await api('GET', `/api/business/emails?status=${status}`);
    mails = r.emails;
    $('#b-smtp-hint', root).classList.toggle('hidden', !!r.smtp_configured);
    $('#b-mail-list', root).innerHTML = mails.length ? mails.map((m) => `<button class="w-full text-left rounded-2xl px-3 py-2.5 hover:bg-white/60 dark:hover:bg-white/[0.06] ${m.email_no === openNo ? 'bg-white/80 dark:bg-white/[0.1]' : ''}" data-open="${m.email_no}" data-row="email-${m.email_no}">
      <div class="flex items-center gap-2"><span class="text-sm font-medium truncate flex-1">${esc(m.to_name || m.to_email || '(no recipient)')}</span><span class="${STATUS[m.status] || 'badge-gray'}">${esc(m.status)}</span></div>
      <div class="text-[13px] truncate">${esc(m.subject_rendered || '(no subject)')}</div><div class="text-[11px] text-slate-500 truncate">${esc(m.preview || '')}</div>
      <div class="text-[10px] text-slate-400 mt-0.5">${esc(m.ref)} · ${esc(m.kind.replace('_', ' '))} · ${esc(fmtRel(m.created_at))}</div></button>`).join('')
      : '<p class="text-center text-sm text-slate-500 py-10">Nothing here yet</p>';
    applyPendingFocus();
  };
  const openMail = async (no) => {
    openNo = no;
    const { email: m } = await api('GET', `/api/business/emails/${no}`);
    const r = $('#b-reader', root);
    const editable = m.status !== 'sent';
    r.innerHTML = `<div class="max-w-3xl space-y-4"><div class="flex flex-wrap items-center gap-2">${ref(m.ref)}<span class="${STATUS[m.status] || 'badge-gray'}">${esc(m.status)}</span>
        <span class="chip">${esc(m.kind.replace('_', ' '))}</span>${m.source ? `<span class="text-xs text-slate-500">from ${ref(m.source)}</span>` : ''}<span class="flex-1"></span>
        ${editable ? '<button class="btn-ghost btn-sm" id="b-edit">Edit</button><button class="btn-primary btn-sm" id="b-send">Send</button>' : ''}<button class="btn-plain btn-sm text-rose-600" id="b-del">Delete</button></div>
      <div class="card space-y-1 text-sm"><div><span class="text-slate-500">To:</span> ${esc(m.to_name ? `${m.to_name} <${m.to_email || '?'}>` : m.to_email || '(not set)')}</div>
        <div><span class="text-slate-500">Subject:</span> <b>${esc(m.subject_rendered)}</b></div>
        ${m.sent_at ? `<div class="text-xs text-slate-500">Sent ${esc(fmtDate(m.sent_at))}</div>` : ''}${m.error ? `<div class="text-xs text-rose-600">Error: ${esc(m.error)}</div>` : ''}</div>
      <div class="card whitespace-pre-wrap text-[14px] leading-relaxed">${esc(m.body_rendered)}</div></div>`;
    r.querySelector('#b-del').addEventListener('click', async () => {
      if (!(await confirmModal('Delete e-mail?', `${m.ref} will be deleted.`, { danger: true, confirmText: 'Delete' }))) return;
      await api('DELETE', `/api/business/emails/${no}`); r.innerHTML = ''; openNo = null; loadList();
    });
    r.querySelector('#b-send')?.addEventListener('click', async (e) => {
      if (!m.to_email) { toast('Add a recipient first (Edit)', 'warn'); return; }
      busy(e.target, true);
      try { const res = await api('POST', `/api/business/emails/${no}/send`); toast(res.message, res.ok ? 'ok' : 'error', 6000); } catch (ex) { toast(ex.message, 'error', 6000); }
      openMail(no); loadList();
    });
    r.querySelector('#b-edit')?.addEventListener('click', () => editMail(m, () => { openMail(no); loadList(); }));
  };
  $('#b-mail-filter', root).addEventListener('click', (e) => { const b = e.target.closest('[data-s]'); if (!b) return; status = b.dataset.s === 'all' ? '' : b.dataset.s; $$('[data-s]', root).forEach((x) => x.classList.toggle('active', x === b)); loadList(); });
  $('#b-mail-list', root).addEventListener('click', (e) => { const b = e.target.closest('[data-open]'); if (b) { openMail(Number(b.dataset.open)); loadList(); } });
  $('#b-compose', root).addEventListener('click', () => editMail(null, loadList));
  sub(on('email:changed', (d) => { if (!d.section || d.section === 'business') loadList(); }));
  sub(on('ref:focus', (r) => { if (r.type === 'email') openMail(Number(r.no)); }));
  await loadList();
}

async function editMail(m, after) {
  const f = el(`<form class="grid gap-3"><div class="grid grid-cols-2 gap-3"><div><label class="label">To (e-mail)</label><input class="input" name="to_email" type="email" value="${esc(m?.to_email || '')}"></div>
    <div><label class="label">To (name)</label><input class="input" name="to_name" value="${esc(m?.to_name || '')}"></div></div>
    <div><label class="label">Subject</label><input class="input" name="subject" maxlength="255" value="${esc(m?.subject || '')}"></div>
    <div><label class="label">Body</label><textarea class="textarea font-mono text-[13px]" name="body" rows="12">${esc(m?.body || '')}</textarea>
    <p class="hint">{business_name} is replaced with your current business name when the e-mail is sent.</p></div></form>`);
  const ok = await modal({ title: m ? `Edit ${m.ref}` : 'Compose e-mail', body: f, wide: true, actions: [{ label: 'Cancel', value: false }, { label: 'Save draft', kind: 'primary', value: true }] });
  if (!ok) return;
  const d = Object.fromEntries(new FormData(f).entries());
  try {
    if (m) await api('PATCH', `/api/business/emails/${m.email_no}`, d); else await api('POST', '/api/business/emails', d);
    toast('Draft saved', 'ok'); after && after();
  } catch (e) { toast(e.message, 'error'); }
}

// ============================================================ News
async function mountNews(root) {
  root.innerHTML = shell(`${header('World & market news', 'One AI digest per day: what is going on in the world, the markets, and the opportunities for your business.',
    '<select id="b-news-day" class="select !w-44 !h-9"><option value="">Today</option></select><button class="btn-ghost" id="b-news-refresh">Refresh</button>')}
    <div id="b-news"></div>`);
  let waiting = null;
  const paintGenerating = (rid, pct = 0, detail = 'starting') => {
    waiting = rid;
    $('#b-news', root).innerHTML = `<div class="panel p-8 text-center"><div class="text-3xl">🗞️</div><h3 class="mt-2 font-semibold">Preparing today's digest…</h3>
      <p class="text-sm text-slate-500">Reading world, market and industry feeds, then analysing them for your business. This runs in the background - you can leave this tab.</p>
      <div class="mt-4 h-1.5 max-w-sm mx-auto rounded-full bg-slate-900/10 dark:bg-white/10 overflow-hidden"><div class="h-full bg-accent-500 transition-all" data-w="${Math.round(pct * 100)}"></div></div>
      <p class="mt-2 text-xs text-slate-500">${esc(detail)} · task ${esc((rid || '').slice(0, 8))}</p></div>`;
    setWidths(root);
  };
  const paint = (d) => {
    waiting = null;
    const byCat = {};
    for (const h of d.headlines) (byCat[h.category] = byCat[h.category] || []).push(h);
    $('#b-news', root).innerHTML = `<div class="space-y-5">
      <section class="panel p-6"><div class="flex flex-wrap items-center gap-2"><h3 class="font-semibold text-lg">Market summary · ${esc(d.date)}</h3>
        <span class="${SENT[d.sentiment] || 'badge-gray'}">${esc(d.sentiment)}</span><span class="text-xs text-slate-500">${d.feeds?.ok?.length || 0} feeds · ${d.headlines.length} headlines</span></div>
        <p class="mt-3 leading-relaxed">${esc(bizvars(d.market_summary))}</p>
        ${d.watchlist?.length ? `<div class="mt-3 flex flex-wrap gap-1.5">${d.watchlist.map((w) => `<span class="chip">👁 ${esc(w)}</span>`).join('')}</div>` : ''}</section>
      <section><h3 class="font-semibold mb-2">The world right now</h3><div class="grid sm:grid-cols-2 xl:grid-cols-3 gap-3">${d.world.map((w) => `<div class="card"><div class="text-xs uppercase tracking-wide text-accent-600 dark:text-accent-300 font-semibold">${esc(w.region)}</div>
        <div class="mt-1 font-medium text-sm">${esc(w.headline)}</div><div class="mt-1 text-xs text-slate-600 dark:text-slate-300">${esc(bizvars(w.impact))}</div></div>`).join('') || '<p class="text-sm text-slate-500">-</p>'}</div></section>
      <section class="grid lg:grid-cols-2 gap-4"><div><h3 class="font-semibold mb-2">Opportunities for ${esc(bizvars('{business_name}'))}</h3><div class="space-y-2">${d.opportunities.map((o) => `<div class="card"><div class="flex items-center gap-2"><b class="text-sm">💡 ${esc(bizvars(o.title))}</b>${o.horizon ? `<span class="chip">${esc(o.horizon)} term</span>` : ''}</div>
        <p class="mt-1 text-sm text-slate-600 dark:text-slate-300">${esc(bizvars(o.why))}</p>${o.action ? `<p class="mt-1 text-sm"><span class="text-emerald-600 font-medium">Next step:</span> ${esc(bizvars(o.action))}</p>` : ''}</div>`).join('') || '<p class="text-sm text-slate-500">-</p>'}</div></div>
        <div><h3 class="font-semibold mb-2">Risks to watch</h3><div class="space-y-2">${d.risks.map((r) => `<div class="card !bg-rose-500/5"><b class="text-sm">⚠️ ${esc(r.title)}</b><p class="mt-1 text-sm text-slate-600 dark:text-slate-300">${esc(bizvars(r.why))}</p></div>`).join('') || '<p class="text-sm text-slate-500">-</p>'}</div></div></section>
      <section class="panel p-6"><h3 class="font-semibold mb-3">Headlines</h3><div class="grid lg:grid-cols-2 gap-x-6 gap-y-4">${Object.entries(byCat).map(([cat, hs]) => `<div><div class="text-xs uppercase tracking-wide text-slate-500 font-semibold mb-1">${esc(cat)}${cat === 'industry' && d.industry_query ? ` · “${esc(d.industry_query)}”` : ''}</div>
        <ul class="space-y-1.5">${hs.slice(0, 10).map((h) => `<li class="text-sm"><a href="${esc(h.link)}" target="_blank" rel="noopener noreferrer" class="no-underline text-slate-800 dark:text-slate-100 hover:underline">${esc(h.title)}</a>
          <span class="text-[11px] text-slate-500">· ${esc(h.source)}${h.published ? ' · ' + esc(fmtRel(h.published)) : ''}</span></li>`).join('')}</ul></div>`).join('')}</div></section></div>`;
  };
  const load = async (day = '') => {
    try {
      const r = await api('GET', `/api/business/news${day ? `?day=${day}` : ''}`);
      if (r.status === 'ready') paint(r.digest); else paintGenerating(r.request_id);
    } catch (e) { $('#b-news', root).innerHTML = `<div class="panel p-6 text-sm text-rose-600">${esc(e.message)}</div>`; }
  };
  const loadDays = async () => {
    const r = await api('GET', '/api/business/news/history');
    $('#b-news-day', root).innerHTML = '<option value="">Today</option>' + r.days.map((d) => `<option value="${esc(d.date)}">${esc(d.date)}</option>`).join('');
  };
  $('#b-news-day', root).addEventListener('change', (e) => load(e.target.value));
  $('#b-news-refresh', root).addEventListener('click', async () => {
    const r = await api('POST', '/api/business/news/refresh');
    if (r.status === 'ready') { toast("Today's digest already exists - one digest per day", 'info'); paint(r.digest); } else paintGenerating(r.request_id);
  });
  sub(on('job', (j) => {
    if (j.kind !== 'news' || j.request_id !== waiting) return;
    if (j.status === 'running' || j.status === 'scheduled') {
      paintGenerating(j.request_id, j.progress, j.detail);
    } else if (j.status !== 'completed') {
      $('#b-news', root).innerHTML = `<div class="panel p-6 text-sm">The digest could not be prepared: ${esc(j.detail)} <button class="btn-ghost btn-sm ml-2" id="b-news-retry">Try again</button></div>`;
      $('#b-news-retry', root)?.addEventListener('click', () => load());
    }
  }));
  sub(on('news:ready', () => { load(); loadDays(); }));
  await Promise.all([load(), loadDays()]);
}

// ============================================================ Simulate
function simCard(s) {
  const bars = barsSVG(s.scenarios.flatMap((x) => [{ label: `${x.name} revenue`, value: x.revenue_impact_pct }, { label: `${x.name} costs`, value: x.cost_impact_pct }]));
  const conf = { low: 'badge-red', medium: 'badge-amber', high: 'badge-green' }[s.confidence] || 'badge-gray';
  return `<article class="panel p-6 space-y-4" data-sim="${s.id}"><div class="flex flex-wrap items-start gap-2"><div class="min-w-0 flex-1"><h3 class="font-semibold text-lg">${esc(bizvars(s.title))}</h3>
      <p class="text-xs text-slate-500">What if: ${esc(s.scenario)} · ${esc(fmtRel(s.created_at))} ${s.source ? '· ' + ref(s.source) : ''}</p></div>
      <span class="${conf}">confidence ${esc(s.confidence)}</span><button class="btn-plain btn-sm text-rose-600" data-del-sim="${s.id}">Delete</button></div>
    <p class="text-sm leading-relaxed">${esc(bizvars(s.summary))}</p>
    <div class="grid lg:grid-cols-3 gap-3">${s.scenarios.map((x) => `<div class="card !p-3"><div class="flex items-center gap-2"><b class="capitalize">${esc(x.name)} case</b><span class="ml-auto chip">${esc(String(x.probability))}%</span></div>
      <div class="mt-2 h-1.5 rounded-full bg-slate-900/10 dark:bg-white/10 overflow-hidden"><div class="h-full ${x.name === 'best' ? 'bg-emerald-500' : x.name === 'worst' ? 'bg-rose-500' : 'bg-accent-500'}" data-w="${x.probability}"></div></div>
      <p class="mt-2 text-xs"><b>Cash:</b> ${esc(bizvars(x.cash_impact))}</p><p class="text-xs"><b>Timeline:</b> ${esc(x.timeline)}</p><p class="mt-1 text-xs text-slate-600 dark:text-slate-300">${esc(bizvars(x.narrative))}</p></div>`).join('')}</div>
    <div class="grid lg:grid-cols-2 gap-4"><div><h4 class="text-sm font-semibold mb-1">Impact on revenue &amp; costs</h4>${bars}</div>
      <div><h4 class="text-sm font-semibold mb-1">KPIs</h4><ul class="space-y-1 text-sm">${s.kpis.map((k) => `<li>${k.direction === 'up' ? '↗️' : k.direction === 'down' ? '↘️' : '→'} <b>${esc(k.name)}</b>: ${esc(k.current || '?')} → ${esc(k.projected)}</li>`).join('') || '<li class="text-slate-500">-</li>'}</ul></div></div>
    <div class="grid lg:grid-cols-2 gap-4"><div><h4 class="text-sm font-semibold mb-1">Recommendations</h4><ol class="list-decimal pl-5 text-sm space-y-1">${s.recommendations.map((r) => `<li>${esc(r)}</li>`).join('')}</ol></div>
      <div><h4 class="text-sm font-semibold mb-1">Assumptions</h4><ul class="list-disc pl-5 text-xs text-slate-600 dark:text-slate-300 space-y-0.5">${s.assumptions.map((a) => `<li>${esc(a)}</li>`).join('')}</ul></div></div></article>`;
}

async function mountSimulate(root) {
  root.innerHTML = shell(`${header('What-if simulator', 'Model best / base / worst outcomes of a decision or event, grounded in your statements, reminders and business summary.')}
    <form id="b-sim-form" class="panel p-4 flex flex-col md:flex-row gap-3"><textarea class="textarea flex-1" name="scenario" rows="2" maxlength="1500" required placeholder="What if we raise our prices by 15% next month?"></textarea>
      <button class="btn-primary md:self-end">Run simulation</button></form>
    <div id="b-sim-running" class="space-y-2"></div><div id="b-sims" class="space-y-4"></div>`);
  const running = new Map();
  const paintRunning = () => {
    $('#b-sim-running', root).innerHTML = [...running.values()].map((j) => `<div class="card flex items-center gap-3 text-sm"><span class="dot-typing text-accent-500"><span></span><span></span><span></span></span>
      <span class="flex-1 truncate">Simulating: ${esc(j.scenario)}</span><span class="text-xs text-slate-500">${esc(j.detail || 'running')}</span></div>`).join('');
  };
  const load = async () => {
    const r = await api('GET', '/api/business/simulations');
    for (const j of r.running) if (!running.has(j.request_id)) running.set(j.request_id, { scenario: j.scenario });
    paintRunning();
    $('#b-sims', root).innerHTML = r.simulations.map(simCard).join('') || '<p class="text-center text-sm text-slate-500 py-10">No simulations yet - try one above or ask your AI "what if sales drop 30%?"</p>';
    setWidths(root);
  };
  $('#b-sim-form', root).addEventListener('submit', async (e) => {
    e.preventDefault();
    const scenario = new FormData(e.target).get('scenario').trim();
    if (!scenario) return;
    try { const r = await api('POST', '/api/business/simulations', { scenario }); running.set(r.request_id, { scenario }); paintRunning(); e.target.reset(); toast('Simulation running in the background', 'info'); } catch (ex) { toast(ex.message, 'warn'); }
  });
  $('#b-sims', root).addEventListener('click', async (e) => {
    const b = e.target.closest('[data-del-sim]'); if (!b) return;
    if (!(await confirmModal('Delete simulation?', 'This what-if result will be removed.', { danger: true, confirmText: 'Delete' }))) return;
    await api('DELETE', `/api/business/simulations/${b.dataset.delSim}`); load();
  });
  sub(on('job', (j) => {
    if (j.kind !== 'simulation') return;
    if (['running', 'scheduled'].includes(j.status)) { running.set(j.request_id, { scenario: j.meta?.scenario || '', detail: j.detail }); paintRunning(); return; }
    running.delete(j.request_id); paintRunning();
    if (j.status === 'completed') load(); else toast(`Simulation ${j.status}: ${j.detail}`, 'warn', 6000);
  }));
  await load();
}

// ============================================================ Summary
async function mountSummary(root) {
  root.innerHTML = shell(`${header('AI business summary', 'Built from what you tell your AI ("running low", "got funding from...", payments, losses) - stored in the summaries table (type = business).',
    '<button class="btn-primary" id="b-regen">Regenerate</button>')}<div id="b-sum"></div>`);
  let regen = null;
  const list = (title, items, icon) => (items && items.length ? `<div><h4 class="text-sm font-semibold mb-1">${title}</h4><ul class="space-y-1 text-sm">${items.map((i) => `<li>${icon} ${esc(bizvars(typeof i === 'string' ? i : JSON.stringify(i)))}</li>`).join('')}</ul></div>` : '');
  const load = async () => {
    const r = await api('GET', '/api/business/summary');
    regen = r.regenerating;
    const c = r.current;
    const d = c?.data || {};
    $('#b-sum', root).innerHTML = `<div class="space-y-5">
      <div class="flex flex-wrap items-center gap-2 text-xs text-slate-500"><span class="chip">business context v${r.business_version}</span><span>${esc(r.business_name || 'no business name set')}</span>
        ${regen ? '<span class="badge-blue">regenerating…</span>' : ''}</div>
      ${c ? `<section class="panel p-6 space-y-4"><div class="flex flex-wrap items-center gap-2"><h3 class="font-semibold text-lg">Current status</h3>
          <span class="${HEALTH[d.financial_health] || 'badge-gray'}">${esc((d.financial_health || 'unknown').replace('_', ' '))}</span>
          <span class="text-xs text-slate-500">${esc(fmtDate(c.created_at))} ${c.ref ? '· from ' + ref(c.ref) : ''}</span></div>
        <p class="leading-relaxed">${esc(bizvars(c.content))}</p>
        ${d.cash_position ? `<div class="card !p-3 text-sm"><b>Cash position:</b> ${esc(bizvars(d.cash_position))}</div>` : ''}
        <div class="grid md:grid-cols-2 gap-5">${list('Key events', d.key_events, '•')}${list('Risks', d.risks, '⚠️')}${list('Opportunities', d.opportunities, '💡')}${list('Next actions', d.next_actions, '✅')}</div></section>`
        : '<div class="panel p-8 text-center text-sm text-slate-500">No business summary yet. Tell your AI how things are going (e.g. "we\'re running low on cash" or "we got 2 lakh from an investor") or press Regenerate.</div>'}
      ${r.history.length ? `<section><h3 class="font-semibold mb-2">History</h3><div class="space-y-2">${r.history.map((h) => `<details class="card"><summary class="cursor-pointer text-sm"><span class="${HEALTH[h.data?.financial_health] || 'badge-gray'}">${esc(h.data?.financial_health || 'unknown')}</span> ${esc(fmtDate(h.created_at))} ${h.ref ? '· ' + ref(h.ref) : ''}</summary>
        <p class="mt-2 text-sm">${esc(bizvars(h.content))}</p></details>`).join('')}</div></section>` : ''}</div>`;
  };
  $('#b-regen', root).addEventListener('click', async (e) => {
    busy(e.target, true, 'Starting');
    try { const r = await api('POST', '/api/business/summary/regenerate'); regen = r.request_id; toast('Regenerating the business summary in the background', 'info'); load(); } catch (ex) { toast(ex.message, 'error'); } finally { busy(e.target, false); }
  });
  sub(on('summary:updated', (d) => { if (d.type === 'business') load(); }));
  sub(on('job', (j) => { if (j.kind === 'summary' && j.request_id === regen && !['running', 'scheduled'].includes(j.status)) { if (j.status !== 'completed') toast(`Summary ${j.status}: ${j.detail}`, 'warn', 6000); load(); } }));
  await load();
}

// ============================================================ exports
const wrap = (fn) => ({ mount: async (root) => { unsubAll(); await fn(root); }, unmount: unsubAll });

export const tabs = [
  { id: 'clients', label: 'Clients', ...wrap(mountClients) },
  { id: 'reminders', label: 'Reminders', ...wrap(mountReminders) },
  { id: 'statements', label: 'Statements', ...wrap(mountStatements) },
  { id: 'inbox', label: 'Inbox', ...wrap(mountInbox) },
  { id: 'news', label: 'News', ...wrap(mountNews) },
  { id: 'simulate', label: 'Simulate', ...wrap(mountSimulate) },
  { id: 'summary', label: 'Summary', ...wrap(mountSummary) },
];

export function init(ctx) {
  C = ctx;
  on('ref:focus', (r) => {
    if (r.section !== 'business' || !['client', 'reminder', 'txn', 'email'].includes(r.type)) return;
    setTimeout(() => focusRow(r.type, r.no), 120);
  });
}

// ------------------------------------------------------------ chat cards
export function registerCards(registerCard) {
  registerCard('client_added', (c) => cardShell(`<div class="flex items-center gap-2">🤝 <b>${c.existed ? 'Client' : 'New client'}:</b> ${esc(c.name)}${c.company ? ` <span class="text-slate-500">(${esc(c.company)})</span>` : ''} ${ref(`client#${c.client_no}`)}</div>
    ${c.reason ? `<div class="mt-1 text-xs"><span class="text-slate-500">Why:</span> ${esc(c.reason)}</div>` : ''}<div class="mt-1 text-[11px] text-slate-500">${c.existed ? 'already existed - details merged' : 'linked to ' + esc(c.source || 'this chat')}</div>`));
  registerCard('client_removed', (c) => cardShell(`🗑️ Removed <b>client#${c.client_no} ${esc(c.name)}</b> · ${c.reminders_removed} reminder(s) removed with it${c.jobs_cancelled ? ` · ${c.jobs_cancelled} scheduled task(s) cancelled` : ''}`));
  registerCard('client_list', (c) => cardShell(`<div class="font-semibold mb-1">👥 Clients (${c.clients.length})</div>${c.clients.map((x) => `<div class="flex items-center gap-2 text-xs py-0.5">${ref(x.ref)}<span class="flex-1 truncate">${esc(x.name)}${x.company ? ' · ' + esc(x.company) : ''}</span>
    ${x.pending_reminders ? `<span class="badge-amber">${esc(Object.values(x.pending_totals).join(', ') || x.pending_reminders + ' pending')}</span>` : ''}</div>`).join('') || '<div class="text-xs text-slate-500">No clients yet</div>'}`));
  registerCard('reminder', (c) => cardShell(`<div class="flex flex-wrap items-center gap-2">💰 <b>${esc(c.title)}</b> ${c.amount_fmt ? `<span class="chip">${esc(c.amount_fmt)}</span>` : ''} ${ref(`reminder#${c.reminder_no}`)}</div>
    <div class="mt-1 text-xs text-slate-600 dark:text-slate-300">Due ${esc(c.due_local)}${c.client_no ? ` · ${ref(`client#${c.client_no}`)} ${esc(c.client_name)}` : ''}</div>
    <div class="mt-1 text-[11px] ${c.scheduled ? 'text-emerald-600' : 'text-amber-600'}">${c.scheduled ? 'Scheduled background task · cancellable until 3 min before' : 'Not scheduled (no future due date)'}</div>`));
  registerCard('reminder_list', (c) => cardShell(`<div class="font-semibold mb-1">⏰ Pending payments (${c.reminders.length})</div>${c.reminders.map((r) => `<div class="flex items-center gap-2 text-xs py-0.5">${ref(r.ref)}<span class="flex-1 truncate">${esc(r.title)} ${r.client_name ? '· ' + esc(r.client_name) : ''}</span><b>${esc(r.amount_fmt)}</b>
    <span class="${r.overdue ? 'text-rose-600' : 'text-slate-500'}">${esc(r.due_local || 'no date')}</span></div>`).join('') || '<div class="text-xs text-slate-500">Nothing pending</div>'}`));
  registerCard('transaction', (c) => cardShell(`<div class="flex flex-wrap items-center gap-2"><span class="${c.kind === 'credit' ? 'badge-green' : 'badge-red'}">${esc(c.kind)}</span>
    <b class="${c.kind === 'credit' ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}">${c.kind === 'credit' ? '+' : '−'}${esc(c.amount_fmt)}</b>
    ${c.counterparty ? `<span>${c.kind === 'credit' ? 'from' : 'to'} ${esc(c.counterparty)}</span>` : ''} ${ref(c.ref)}</div>
    <div class="mt-1 text-[11px] text-slate-500">${esc(c.occurred_on || '')} · added to Statements${c.settled_reminder ? ` · closed ${ref('reminder#' + c.settled_reminder)}` : ''}</div>`));
  registerCard('cold_email', (c) => {
    const node = el(cardShell(`<div class="flex flex-wrap items-center gap-2">✉️ <b>${esc(c.kind === 'cold_sales' ? 'Cold sales e-mail' : 'E-mail')}</b> ${ref(c.ref)}
      <span class="${STATUS[c.status] || 'badge-gray'}" data-st>${esc(c.status)}</span></div>
      <div class="mt-1 text-xs"><span class="text-slate-500">To:</span> ${esc(c.to_name || '')} ${esc(c.to_email ? `<${c.to_email}>` : '(recipient not set)')}</div>
      <div class="text-sm font-medium mt-1">${esc(c.subject_rendered || bizvars(c.subject || ''))}</div>
      <div class="mt-1 text-xs text-slate-600 dark:text-slate-300 whitespace-pre-wrap line-clamp-6">${esc(c.body_rendered || bizvars(c.body || ''))}</div>
      <div class="mt-2 flex gap-2">${c.status !== 'sent' ? '<button class="btn-primary btn-sm" data-send>Send</button>' : ''}<button class="btn-ghost btn-sm" data-ref="${esc(c.ref)}">Open in Sent inbox</button></div>`));
    node.querySelector('[data-send]')?.addEventListener('click', async (e) => {
      if (!c.to_email) { toast('Open it in the Sent inbox and add a recipient first', 'warn'); return; }
      busy(e.target, true);
      try {
        const r = await api('POST', `/api/business/emails/${c.email_no}/send`);
        toast(r.message, r.ok ? 'ok' : 'error', 6000);
        if (r.ok) { node.querySelector('[data-st]').textContent = 'sent'; e.target.remove(); return; }
      } catch (ex) { toast(ex.message, 'error', 6000); }
      busy(e.target, false);
    });
    return node;
  });
  registerCard('news', (c) => cardShell(c.status !== 'ready'
    ? '🗞️ Today\'s news digest is being prepared in the background - it will appear in the <b>News</b> tab.'
    : `<div class="flex items-center gap-2">🗞️ <b>Daily digest ${esc(c.date)}</b><span class="${SENT[c.sentiment] || 'badge-gray'}">${esc(c.sentiment)}</span></div>
      <p class="mt-1 text-xs text-slate-600 dark:text-slate-300">${esc(bizvars(c.market_summary).slice(0, 420))}${c.market_summary.length > 420 ? '…' : ''}</p>
      ${(c.opportunities || []).map((o) => `<div class="mt-1 text-xs">💡 <b>${esc(bizvars(o.title))}</b> - ${esc(bizvars(o.action || o.why || ''))}</div>`).join('')}`));
  registerCard('simulation', (c) => {
    const node = el(cardShell(`<div class="flex items-center gap-2">🔮 <b>${esc(bizvars(c.title))}</b><span class="chip">confidence ${esc(c.confidence)}</span></div>
      <div class="mt-2 grid grid-cols-3 gap-2">${c.scenarios.map((x) => `<div class="rounded-xl glass-soft p-2 text-center"><div class="text-[10px] uppercase text-slate-500">${esc(x.name)}</div>
        <div class="text-sm font-semibold">${esc(String(x.probability))}%</div><div class="text-[10px] ${x.revenue_impact_pct >= 0 ? 'text-emerald-600' : 'text-rose-600'}">rev ${x.revenue_impact_pct > 0 ? '+' : ''}${esc(String(x.revenue_impact_pct))}%</div></div>`).join('')}</div>
      <div class="mt-2">${barsSVG(c.scenarios.map((x) => ({ label: x.name, value: x.revenue_impact_pct - x.cost_impact_pct })), { unit: ' pts' })}</div>
      <div class="mt-1 text-[11px] text-slate-500">Saved in the Simulate tab.</div>`));
    return node;
  });
  registerCard('business_status', (c) => {
    const o = c.overview || {};
    return cardShell(`<div class="flex items-center gap-2">📊 <b>Business status</b>${c.health ? `<span class="${HEALTH[c.health] || 'badge-gray'}">${esc(c.health.replace('_', ' '))}</span>` : ''}</div>
      <div class="mt-2 grid grid-cols-2 sm:grid-cols-4 gap-2 text-center">${[['Clients', o.clients], ['Pending', o.pending_reminders], ['Overdue', o.overdue], ['Drafts', o.drafts]].map(([l, v]) => `<div class="rounded-xl glass-soft p-2"><div class="text-[10px] uppercase text-slate-500">${l}</div><div class="font-semibold">${esc(String(v ?? 0))}</div></div>`).join('')}</div>
      ${Object.values(o.totals || {}).map((t) => `<div class="mt-1 text-xs">${esc(t.currency || '')}: in ${esc(t.credits_fmt)} · out ${esc(t.debits_fmt)} · <b>net ${esc(t.net_fmt)}</b></div>`).join('')}`);
  });
}
