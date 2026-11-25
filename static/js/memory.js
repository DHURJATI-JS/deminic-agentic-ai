// Global memory tab (one per section). Search is semantic (pgvector).
// "Delete all" runs as a NON-cancellable background task protected by a
// restore point; tasks/reminders of the section are kept but marked stale.
import { $, $$, api, confirmModal, debounce, esc, fmtRel, on, toast } from './core.js';

// "instruction" = a standing rule for how the AI behaves (e.g. "Do not reassure me") - applied to every reply
const CATS = ['health', 'finance', 'contact', 'preference', 'work', 'legal', 'instruction', 'general'];
const ICON = { health: '❤️', finance: '💰', contact: '👤', preference: '⭐', work: '💼', legal: '⚖️', instruction: '🧭', general: '📝' };
let root = null; let C = null; let items = []; const unsubs = []; const revealed = new Set();

export async function mount(el, ctx) {
  root = el; C = ctx;
  el.innerHTML = `<div class="h-full overflow-y-auto scroll-thin p-5 md:p-8">
    <div class="flex flex-wrap items-center gap-3">
      <div><h2 class="text-xl font-semibold">Global memory &middot; ${esc(ctx.section)}</h2>
      <p class="text-sm text-slate-500">What your AI remembers in this mode. Retrieved by vector search; health &amp; finance are encrypted at rest.</p></div>
      <span class="flex-1"></span>
      <input id="m-search" class="input !w-64" placeholder="Semantic search…" aria-label="Search memory">
      <button id="m-wipe" class="btn-danger">Delete all ${esc(ctx.section)} memory</button>
    </div>
    <div id="m-wipe-state" class="hidden mt-4 card !bg-amber-500/10 text-sm"></div>
    <form id="m-add" class="mt-5 card grid md:grid-cols-[150px_1fr_2fr_auto] gap-2 items-end">
      <div><label class="label">Category</label><select class="select" name="category">${CATS.filter((c) => c !== 'contact').map((c) => `<option>${c}</option>`).join('')}</select></div>
      <div><label class="label">Label</label><input class="input" name="key" required maxlength="160" placeholder="e.g. peanut allergy"></div>
      <div><label class="label">Value</label><input class="input" name="value" required maxlength="2000" placeholder="e.g. severe - carry an EpiPen"></div>
      <button class="btn-primary">Remember</button>
    </form>
    <div id="m-list" class="mt-5 grid md:grid-cols-2 gap-3"></div>
  </div>`;
  $('#m-add', el).addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api('POST', '/api/memory', { section: ctx.section, category: f.get('category'), key: f.get('key'), value: f.get('value') });
      e.target.reset(); toast('Saved to memory', 'ok'); load();
    } catch (ex) { toast(ex.message, 'error'); }
  });
  $('#m-search', el).addEventListener('input', debounce(search, 300));
  $('#m-wipe', el).addEventListener('click', wipe);
  unsubs.push(on('memory:changed', (d) => { if (d.section === ctx.section) load(); }));
  unsubs.push(on('job', (j) => { if (j.kind === 'memory_wipe' && j.meta?.section === ctx.section) paintWipe(j); }));
  await load();
}

export function unmount() { while (unsubs.length) unsubs.pop()(); root = null; }

async function load() {
  if (!root) return;
  const r = await api('GET', `/api/memory?section=${C.section}`);
  items = r.memories;
  paint(items);
  if (r.wipe_running) paintWipe({ status: 'running', progress: 0.1, detail: 'deleting...' });
}

async function search(e) {
  const q = e.target.value.trim();
  if (!q) return paint(items);
  const r = await api('GET', `/api/memory/search?section=${C.section}&q=${encodeURIComponent(q)}`);
  paint(r.results, true);
}

function paint(list, scored = false) {
  const box = $('#m-list', root);
  if (!box) return;
  if (!list.length) { box.innerHTML = `<p class="text-sm text-slate-500 col-span-2 text-center py-10">${scored ? 'No semantically similar memories' : 'Nothing remembered yet in this mode.'}</p>`; return; }
  box.innerHTML = '';
  for (const m of list) {
    const d = document.createElement('div');
    d.className = 'card flex gap-3 items-start';
    const isContact = m.category === 'contact';
    const shown = !m.sensitive || revealed.has(m.id);
    const value = isContact ? `${esc(m.data?.relation || '')} ${m.data?.email ? '· ' + esc(m.data.email) : ''} ${m.data?.phone ? '· ' + esc(m.data.phone) : ''}` : shown ? esc(m.value) : '•••••• (sensitive)';
    d.innerHTML = `<div class="text-xl">${ICON[m.category] || '📝'}</div>
      <div class="min-w-0 flex-1"><div class="text-sm font-medium">${esc(isContact ? (m.data?.name || m.key) : m.key)}
        <span class="ml-1 ${m.sensitive ? 'badge-amber' : 'badge-gray'}">${esc(m.category)}${m.sensitive ? ' · encrypted' : ''}</span>${scored ? `<span class="ml-1 badge-blue">${m.score}</span>` : ''}</div>
        <div class="text-sm text-slate-600 dark:text-slate-300 break-words">${value}</div>
        <div class="text-[10px] text-slate-400 mt-1">created ${esc(fmtRel(m.created_at))} ${m.source_chat_no ? `· <a class="ref-link" data-ref="chat#${m.source_chat_no}${m.source_msg_no ? '.' + m.source_msg_no : ''}">chat#${m.source_chat_no}${m.source_msg_no ? '.' + m.source_msg_no : ''}</a>` : ''}</div></div>
      <div class="flex flex-col gap-1">${m.sensitive ? `<button class="btn-plain btn-sm" data-reveal="${m.id}">${shown ? 'Hide' : 'Reveal'}</button>` : ''}
        <button class="btn-plain btn-sm text-rose-600" data-del="${m.id}">Delete</button></div>`;
    box.appendChild(d);
  }
  for (const b of $$('[data-reveal]', box)) b.addEventListener('click', () => { const id = Number(b.dataset.reveal); revealed.has(id) ? revealed.delete(id) : revealed.add(id); paint(list, scored); });
  for (const b of $$('[data-del]', box)) b.addEventListener('click', async () => {
    if (!(await confirmModal('Delete this memory?', 'It will be removed from global memory and the vector store.', { danger: true, confirmText: 'Delete' }))) return;
    await api('DELETE', `/api/memory/${b.dataset.del}?section=${C.section}`); load();
  });
}

async function wipe() {
  const ok = await confirmModal(`Delete ALL ${C.section} memory?`,
    'This runs as a protected background task (it cannot be cancelled). If the app closes mid-way it rolls back to a restore point. Tasks and reminders stay in the database but are marked stale and removed from the vector store.',
    { danger: true, confirmText: 'Delete everything in this mode' });
  if (!ok) return;
  try {
    const r = await api('POST', '/api/memory/wipe', { section: C.section });
    paintWipe({ status: 'running', progress: 0.05, detail: `started (request ${r.request_id.slice(0, 8)}, restore point created)` });
  } catch (e) { toast(e.message, 'error'); }
}

function paintWipe(j) {
  const box = root && $('#m-wipe-state', root);
  if (!box) return;
  box.classList.remove('hidden');
  const pct = Math.round((j.progress || 0) * 100);
  box.innerHTML = `<div class="flex items-center gap-2"><b>Memory deletion</b><span class="badge-amber">protected</span><span class="ml-auto">${esc(j.status)} · ${pct}%</span></div>
    <div class="mt-2 h-1.5 rounded-full bg-slate-900/10 overflow-hidden"><div class="h-full bg-amber-500" data-w></div></div><div class="mt-1 text-xs">${esc(j.detail || '')}</div>`;
  box.querySelector('[data-w]').style.width = `${Math.max(4, pct)}%`;
  if (j.status !== 'running') { setTimeout(() => box.classList.add('hidden'), 6000); load(); }
}
