// Chat view - generation runs as a background job on the server, so switching
// chats/tabs never stops it: deltas stream over Socket.IO keyed by request id
// and are re-attached when you come back.
import { $, $$, api, bizvars, cancelJob, enhance, esc, fmtRel, getLocation, md, on, state, toast, uuid } from './core.js';
import { renderCards } from './cards.js';
import { icon } from './icons.js';

const current = { personal: null, business: null, coding: null };
const streams = new Map(); // request_id -> { chat_no, msg_no, buf, status, stage }
let C = null; // app ctx
let root = null;
let data = null; // current chat payload
let renderQueued = false;
const unsubs = [];

const STAGES = { thinking: 'Thinking', planning: 'Choosing tools', tool: 'Using', writing: 'Writing', escalating: 'Handing over to' };
const SUGGEST = {
  personal: ['Remind me to call mom at 7pm', "What's the weather in Delhi tomorrow?", 'Play some synthwave', 'Recommend a feel-good movie',
    'Create a profile for my brother Rahul, rahul@example.com', "I'm in Jaipur - what local dishes should I try?", 'Prepare questions for my custody hearing'],
  business: ['Add client Acme Corp - they owe us 2,000 by Friday', 'We received 50,000 from Zenith Ventures today', 'Draft a cold sales e-mail for fintech startups',
    'What if we raise our prices by 15%?', "What's happening in the markets today?", "I'm running low on cash this month"],
  coding: ['Write a FastAPI endpoint for safe file uploads', 'Is requests 2.19.0 safe to use?', 'Write a Python script that resizes all images in a folder',
    'Explain async/await in JavaScript with an example', 'Review this SQL for injection risks: ...'],
};

export function currentChatNo(section) { return current[section]; }
export function setCurrent(section, no) { current[section] = no; }
export function clearCurrent(section) { current[section] = null; }

export function init(ctx) {
  C = ctx;
  on('chat:status', (d) => { const s = ensure(d); s.stage = d.stage; s.tool = d.tool || d.provider || ''; paintStream(d.request_id); });
  on('chat:delta', (d) => { const s = ensure(d); s.buf += d.delta || ''; s.stage = 'writing'; queueRender(d.request_id); });
  on('chat:reset', (d) => { const s = ensure(d); s.buf = ''; queueRender(d.request_id); });
  on('chat:done', (d) => {
    const s = ensure(d);
    s.buf = d.reply || s.buf; s.status = d.status; s.cards = d.cards || []; s.provider = d.provider; s.escalated = d.escalated;
    finish(d);
  });
  on('chat:points', (d) => { if (data && d.chat_no === data.chat.chat_no) { data.main_points.push(...d.points); paintPoints(); } });
}

function ensure(d) {
  if (!streams.has(d.request_id)) streams.set(d.request_id, { chat_no: d.chat_no, msg_no: d.msg_no, buf: '', status: 'generating', stage: 'thinking' });
  const s = streams.get(d.request_id);
  if (d.msg_no) s.msg_no = d.msg_no;
  return s;
}

export async function mount(el, ctx) {
  C = ctx;
  root = el;
  const section = ctx.section;
  el.innerHTML = `<div class="h-full flex">
    <div class="flex-1 min-w-0 flex flex-col">
      <div class="h-14 shrink-0 px-4 flex items-center gap-2 border-b border-slate-900/[0.06] dark:border-white/[0.07]">
        <div class="min-w-0"><div id="c-title" class="font-semibold truncate" title="Double-click to rename">${esc(section === 'coding' ? 'Coding chat' : 'New conversation')}</div>
          <div id="c-ref" class="text-[11px] text-slate-500 font-mono"></div></div>
        <span class="flex-1"></span>
        ${section !== 'coding' ? `<label class="chip cursor-pointer select-none" title="Lawyer mode: scenario reconstruction + sharp trial questions"><input type="checkbox" id="c-lawyer" class="mr-1">⚖️ Lawyer</label>` : `
          <select id="c-provider" class="select !h-8 !w-auto !text-xs" title="Coding model (Gemma is never used for code)">
            <option value="auto">Qwen (local) → online fallback</option><option value="local">Local Qwen only</option>
            <option value="claude">Claude</option><option value="openai">ChatGPT</option><option value="github">GitHub Models</option></select>`}
        <button id="c-points-toggle" class="btn-ghost btn-sm" title="Main points">✦ Points</button>
      </div>
      <div id="c-outdated" class="hidden px-4 py-2.5 bg-rose-500/10 border-b border-rose-500/25 text-sm text-rose-700 dark:text-rose-300 flex items-center gap-3">
        <span>⚠️ This chat is linked to your <b>previous business description</b>. Old context may be wrong - please start a new chat.</span>
        <button id="c-new-from-outdated" class="btn-danger btn-sm ml-auto">Start new chat</button></div>
      <div id="c-msgs" class="flex-1 overflow-y-auto scroll-thin px-3 md:px-8 py-6 space-y-6"></div>
      <form id="c-form" class="shrink-0 p-3 md:px-8 md:pb-5">
        <div id="c-atts" class="hidden flex-wrap gap-1.5 px-3 pb-2"></div>
        <div class="composer">
          <button type="button" id="c-attach" class="btn-plain btn-icon shrink-0" title="Attach a file or image to analyse (ClamAV scanned)" aria-label="Attach file">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m21.4 11.1-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5"/></svg></button>
          <input type="file" id="c-file" class="hidden" multiple accept=".txt,.md,.csv,.tsv,.json,.pdf,.py,.js,.ts,.html,.xml,.yaml,.yml,.log,.sql,.java,.c,.cpp,.go,.rs,.rb,.php,.css,image/*">
          <button type="button" id="c-mic" class="btn-plain btn-icon shrink-0" title="Speech to text" aria-label="Dictate">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0M12 17v5"/></svg></button>
          <textarea id="c-input" rows="1" class="flex-1 resize-none bg-transparent px-2 py-2 text-[15px] focus:outline-none max-h-48 scroll-thin" placeholder="Message ${esc(state.me.user.ai_name)}…" aria-label="Message"></textarea>
          <button type="button" id="c-loc" class="btn-plain btn-icon shrink-0" title="Share location with this message" aria-label="Location">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/></svg></button>
          <button type="submit" id="c-send" class="btn-primary btn-icon !rounded-full shrink-0" aria-label="Send">
            <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6"><path d="M12 19V5M5 12l7-7 7 7"/></svg></button>
          <button type="button" id="c-stop" class="btn-danger btn-icon !rounded-full shrink-0 hidden" aria-label="Stop generating" title="Stop (cancels this request id)">
            <svg class="w-4 h-4" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg></button>
        </div>
        <div class="mt-1.5 px-3 flex items-center gap-2 text-[11px] text-slate-500">
          <span>${esc(section)} mode</span><span>·</span><span id="c-loc-state">location off</span><span class="ml-auto hidden sm:inline">Enter to send · Shift+Enter new line</span></div>
      </form>
    </div>
    <aside id="c-points" class="hidden w-80 shrink-0 border-l border-slate-900/[0.06] dark:border-white/[0.07] flex-col">
      <div class="h-14 px-4 flex items-center gap-2 border-b border-slate-900/[0.06] dark:border-white/[0.07]">
        <h3 class="font-semibold text-sm">Main points</h3>
        <div class="seg ml-auto !p-0.5"><button class="seg-item !h-6 !text-[11px] active" data-pts="chat">This chat</button><button class="seg-item !h-6 !text-[11px]" data-pts="all">All chats</button></div>
      </div>
      <div id="c-points-list" class="flex-1 overflow-y-auto scroll-thin p-3 space-y-2 text-sm"></div>
    </aside>
  </div>`;
  bindComposer();
  const no = current[section];
  if (no) await open(no); else paintEmpty();
}

export function unmount() { while (unsubs.length) unsubs.pop()(); root = null; data = null; }

function paintEmpty() {
  const box = $('#c-msgs', root);
  const section = C.section;
  const SUGGEST_ICON = { personal: ['bell', 'cloud-sun', 'music', 'film', 'users', 'map-pin', 'scale'],
    business: ['briefcase', 'wallet', 'mail', 'trending-up', 'newspaper', 'alert'], coding: ['code', 'shield-check', 'image', 'zap', 'database'] };
  // min-h-full (not h-full): on short screens the welcome grows and scrolls instead of being clipped at the top
  box.innerHTML = `<div class="min-h-full flex flex-col items-center justify-center text-center px-4 py-6 stagger-in">
    <div class="ai-avatar !w-16 !h-16 !rounded-[20px] !p-[3px]"><img src="/static/img/logo.svg" alt="" class="!rounded-[17px]"></div>
    <h2 class="mt-5 text-3xl font-semibold tracking-tight">Hi ${esc(state.me.user.name.split(' ')[0])}, I'm <span class="gradient-text">${esc(state.me.user.ai_name)}</span>.</h2>
    <p class="mt-1.5 text-sm text-slate-500">${section === 'business' ? 'Your business desk' : section === 'coding' ? 'Your coding studio · Qwen or online models only' : 'Your personal assistant'} &middot; type below or pick an idea</p>
    <div class="mt-7 grid sm:grid-cols-2 gap-2.5 w-full max-w-2xl text-left">${SUGGEST[section].map((s, i) => `<button class="card card-hover !p-3 flex items-center gap-3 text-sm text-left text-slate-700 dark:text-slate-200" data-suggest="${esc(s)}">
      <span class="icon-badge-soft !w-8 !h-8 !rounded-lg">${icon(SUGGEST_ICON[section][i % SUGGEST_ICON[section].length], 'w-4 h-4')}</span><span class="flex-1">${esc(s)}</span>${icon('chevron-right', 'w-4 h-4 text-slate-400')}</button>`).join('')}</div></div>`;
  for (const b of $$('[data-suggest]', box)) b.addEventListener('click', () => { $('#c-input', root).value = b.dataset.suggest; $('#c-input', root).focus(); autosize(); });
  $('#c-title', root).textContent = 'New conversation';
  $('#c-ref', root).textContent = '';
  $('#c-outdated', root).classList.add('hidden');
  setComposerEnabled(true);
  paintPoints();
}

export async function open(no) {
  if (!root) return;
  current[C.section] = no;
  try { data = await api('GET', `/api/chats/${no}`); } catch (e) {
    if (e.status === 404) { current[C.section] = null; paintEmpty(); return; }
    toast(e.message, 'error'); return;
  }
  if (data.chat.section !== C.section) { current[C.section] = null; paintEmpty(); return; }
  $('#c-title', root).textContent = bizvars(data.chat.title);
  $('#c-ref', root).textContent = `${data.chat.ref} · ${data.messages.length} message${data.messages.length === 1 ? '' : 's'}`;
  const lw = $('#c-lawyer', root);
  if (lw) lw.checked = !!data.chat.lawyer_mode;
  $('#c-outdated', root).classList.toggle('hidden', !data.chat.outdated);
  $('#c-outdated', root).classList.toggle('flex', !!data.chat.outdated);
  setComposerEnabled(!data.chat.outdated);
  const box = $('#c-msgs', root);
  box.innerHTML = '';
  for (const m of data.messages) {
    if (m.status === 'generating' && m.request_id) {
      const s = streams.get(m.request_id) || { chat_no: no, msg_no: m.chatno, buf: data.partial || '', status: 'generating', stage: 'writing' };
      if (!s.buf && data.partial) s.buf = data.partial;
      streams.set(m.request_id, s);
    }
    const el = messageEl(m);
    el.style.animationDelay = `${Math.min(data.messages.indexOf(m), 12) * 35}ms`;
    box.appendChild(el);
  }
  if (!data.messages.length) box.innerHTML = `<div class="pt-20 text-center"><span class="icon-badge-soft mx-auto">${icon('message', 'w-4 h-4')}</span><p class="mt-3 text-sm text-slate-500">Empty chat — say something!</p></div>`;
  box.scrollTop = box.scrollHeight;
  updateStopButton();
  paintPoints();
}

export function scrollToMessage(no, msgNo) {
  setTimeout(() => {
    const t = document.getElementById(`msg-${no}-${msgNo}`);
    if (t) { t.scrollIntoView({ behavior: 'smooth', block: 'center' }); t.classList.add('ring-2', 'ring-accent-500/50', 'rounded-3xl'); setTimeout(() => t.classList.remove('ring-2', 'ring-accent-500/50'), 2500); }
  }, 250);
}

function messageEl(m) {
  const no = data ? data.chat.chat_no : current[C.section];
  const wrap = document.createElement('div');
  wrap.className = 'space-y-2 stagger-in group';
  wrap.id = `msg-${no}-${m.chatno || 'new'}`;
  wrap.dataset.rid = m.request_id || '';
  const ref = m.chatno ? `chat#${no}.${m.chatno}` : '';
  wrap.innerHTML = `
    <div class="flex justify-end"><div class="bubble-user"><div class="whitespace-pre-wrap break-words text-[14.5px]">${esc(m.prompt)}</div>
      ${(m.attachments || []).length ? `<div class="mt-1.5 flex flex-wrap gap-1 justify-end">${m.attachments.map((a) => `<span class="inline-flex items-center gap-1 rounded-full bg-white/20 px-2 h-5 text-[11px]">${a.kind === 'image' ? '🖼️' : '📄'} ${esc(a.name)}</span>`).join('')}</div>` : ''}</div></div>
    <div class="flex justify-end gap-2 text-[10px] text-slate-400 pr-1">${ref ? `<a class="ref-link" data-ref="${ref}" title="Message reference">${ref}</a>` : ''}<span>${esc(fmtRel(m.created_at))}</span></div>
    <div class="flex gap-2.5 items-start"><span class="ai-avatar mt-0.5"><img src="/static/img/logo.svg" alt=""></span>
      <div class="bubble-ai min-w-0"><div class="md ai-body"></div><div class="ai-stage text-xs text-slate-500 hidden"></div><div class="ai-cards"></div></div>
      <div class="msg-actions flex flex-col gap-1 pt-1"><button class="btn-plain btn-icon btn-sm" data-copy title="Copy reply" aria-label="Copy reply">${icon('copy', 'w-3.5 h-3.5')}</button></div></div>
    <div class="ai-meta pl-11 flex flex-wrap items-center gap-1.5 text-[10px] text-slate-400"></div>`;
  wrap.querySelector('[data-copy]').addEventListener('click', () => {
    const txt = wrap.querySelector('.ai-body')?.innerText || '';
    navigator.clipboard?.writeText(txt).then(() => toast('Reply copied', 'ok', 1500), () => toast('Copy failed', 'warn'));
  });
  paintMessage(wrap, m);
  return wrap;
}

function paintMessage(wrap, m) {
  const body = wrap.querySelector('.ai-body');
  const stage = wrap.querySelector('.ai-stage');
  const s = m.request_id ? streams.get(m.request_id) : null;
  const generating = m.status === 'generating' && s && s.status === 'generating';
  const text = generating ? s.buf : (s && s.status !== 'generating' && s.buf && !m.reply ? s.buf : m.reply);
  if (text) { body.innerHTML = md(text); enhance(body, text, C ? C.section : ''); } else body.innerHTML = '';
  if (generating) {
    stage.classList.remove('hidden');
    const label = STAGES[s.stage] || 'Working';
    stage.innerHTML = `<span class="dot-typing text-accent-500"><span></span><span></span><span></span></span> ${esc(label)}${s.tool ? ' ' + esc(s.tool) : ''}…`;
  } else stage.classList.add('hidden');
  renderCards(wrap.querySelector('.ai-cards'), (s && s.cards) || m.cards || [], C);
  const meta = wrap.querySelector('.ai-meta');
  const prov = (s && s.provider) || m.provider;
  const esc8 = (s && s.escalated) || m.escalated;
  const st = (s && s.status !== 'generating' ? s.status : m.status);
  const provIcon = esc8 ? 'zap' : /claude|chatgpt|github|openai/i.test(prov || '') ? 'cloud' : /fallback/i.test(prov || '') ? 'shield' : 'cpu';
  meta.innerHTML = `${prov ? `<span class="chip !h-5">${icon(provIcon, 'w-3 h-3')}${esc(prov)}</span>` : ''}${esc8 ? `<span class="badge-violet">${icon('zap', 'w-3 h-3')}escalated: beyond local limits</span>` : ''}
    ${st === 'cancelled' ? '<span class="badge-gray">cancelled</span>' : ''}${st === 'error' ? '<span class="badge-red">error</span>' : ''}
    ${(m.refs || []).map((r) => `<a class="ref-link" data-ref="${esc(r)}">${esc(r)}</a>`).join('')}`;
}

function queueRender(rid) {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; for (const id of [...streams.keys()]) paintStream(id); });
}

function paintStream(rid) {
  if (!root || !data) return;
  const s = streams.get(rid);
  if (!s || s.chat_no !== data.chat.chat_no) return;
  const wrap = root.querySelector(`[data-rid="${CSS.escape(rid)}"]`);
  if (!wrap) return;
  const m = data.messages.find((x) => x.request_id === rid) || { request_id: rid, status: 'generating' };
  paintMessage(wrap, { ...m, status: s.status === 'generating' ? 'generating' : s.status });
  const box = $('#c-msgs', root);
  if (box.scrollHeight - box.scrollTop - box.clientHeight < 220) box.scrollTop = box.scrollHeight;
}

function finish(d) {
  const s = streams.get(d.request_id);
  if (data && d.chat_no === data.chat.chat_no) {
    const m = data.messages.find((x) => x.request_id === d.request_id);
    if (m) { m.reply = d.reply; m.status = d.status; m.cards = d.cards || []; m.refs = d.refs || []; m.provider = d.provider; m.escalated = d.escalated; }
    paintStream(d.request_id);
    updateStopButton();
  }
  if (s) setTimeout(() => streams.delete(d.request_id), 60000);
}

function runningRequest() {
  if (!data) return null;
  const m = [...data.messages].reverse().find((x) => x.status === 'generating' && x.request_id && (streams.get(x.request_id)?.status ?? 'generating') === 'generating');
  return m ? m.request_id : null;
}
function updateStopButton() {
  if (!root) return;
  const rid = runningRequest();
  $('#c-stop', root).classList.toggle('hidden', !rid);
  $('#c-send', root).classList.toggle('hidden', !!rid);
}
function setComposerEnabled(on) {
  const i = $('#c-input', root);
  i.disabled = !on;
  i.placeholder = on ? `Message ${state.me.user.ai_name}…` : 'This chat is outdated - start a new chat';
  $('#c-send', root).disabled = !on;
}
function autosize() { const i = $('#c-input', root); i.style.height = 'auto'; i.style.height = `${Math.min(i.scrollHeight, 192)}px`; }

let shareLoc = false;
const pendingAtts = []; // uploaded, not yet sent (max 3)

function paintAtts() {
  const box = root && $('#c-atts', root);
  if (!box) return;
  box.classList.toggle('hidden', !pendingAtts.length);
  box.classList.toggle('flex', !!pendingAtts.length);
  box.innerHTML = pendingAtts.map((a, i) => `<span class="chip !h-7">${a.kind === 'image' ? '🖼️' : '📄'} ${esc(a.name)}
    <span class="text-slate-400">${a.kind === 'text' ? `${Number(a.chars).toLocaleString()} chars` : 'image'}</span>
    <button type="button" class="ml-1 hover:text-rose-500" data-rm-att="${i}" aria-label="Remove attachment">&times;</button></span>`).join('');
  for (const b of $$('[data-rm-att]', box)) b.addEventListener('click', () => { pendingAtts.splice(Number(b.dataset.rmAtt), 1); paintAtts(); });
}

async function attachFiles(files) {
  for (const f of files) {
    if (pendingAtts.length >= 3) { toast('Up to 3 attachments per message', 'warn'); break; }
    const fd = new FormData();
    fd.append('file', f);
    try {
      toast(`Scanning ${f.name}…`, 'info', 1500);
      const r = await api('POST', '/api/attachments', fd);
      pendingAtts.push(r.attachment);
      paintAtts();
    } catch (e) { toast(`${f.name}: ${e.message}`, 'error', 6000); }
  }
}
function bindComposer() {
  const input = $('#c-input', root);
  input.addEventListener('input', autosize);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('#c-form', root).requestSubmit(); } });
  $('#c-form', root).addEventListener('submit', (e) => { e.preventDefault(); send(); });
  $('#c-stop', root).addEventListener('click', async () => {
    const rid = runningRequest();
    if (!rid) return;
    const ack = await cancelJob(rid);
    toast(ack.ok ? 'Stopped' : ack.message, ack.ok ? 'ok' : 'warn');
  });
  $('#c-mic', root).addEventListener('click', dictate);
  $('#c-attach', root).addEventListener('click', () => $('#c-file', root).click());
  $('#c-file', root).addEventListener('change', async (e) => { await attachFiles([...e.target.files]); e.target.value = ''; });
  const form = $('#c-form', root);
  form.addEventListener('dragover', (e) => { e.preventDefault(); form.classList.add('ring-2', 'ring-accent-500/40', 'rounded-3xl'); });
  form.addEventListener('dragleave', () => form.classList.remove('ring-2', 'ring-accent-500/40'));
  form.addEventListener('drop', (e) => { e.preventDefault(); form.classList.remove('ring-2', 'ring-accent-500/40'); attachFiles([...e.dataTransfer.files]); });
  input.addEventListener('paste', (e) => { const files = [...(e.clipboardData?.files || [])]; if (files.length) { e.preventDefault(); attachFiles(files); } });
  paintAtts();
  shareLoc = !!state.me.providers.share_location && window.isSecureContext && !!navigator.geolocation;
  const locBtn = $('#c-loc', root);
  const paintLoc = () => { locBtn.classList.toggle('text-accent-500', shareLoc); $('#c-loc-state', root).textContent = shareLoc ? 'location on' : (window.isSecureContext ? 'location off' : 'location needs HTTPS'); };
  locBtn.addEventListener('click', () => {
    if (!window.isSecureContext) { toast('Location needs the HTTPS address (or localhost).', 'warn'); return; }
    shareLoc = !shareLoc; paintLoc(); if (shareLoc) getLocation();
  });
  paintLoc();
  const lw = $('#c-lawyer', root);
  if (lw) lw.addEventListener('change', async () => {
    if (!data) return;
    await api('PATCH', `/api/chats/${data.chat.chat_no}`, { lawyer_mode: lw.checked });
    toast(lw.checked ? 'Lawyer mode on for this chat' : 'Lawyer mode off', 'info');
  });
  const prov = $('#c-provider', root);
  if (prov) { prov.value = state.me.providers.coding_provider || 'auto'; prov.addEventListener('change', () => api('PUT', '/api/settings/providers', { coding_provider: prov.value }).then(() => { state.me.providers.coding_provider = prov.value; toast('Coding model updated', 'ok'); })); }
  $('#c-points-toggle', root).addEventListener('click', () => { const p = $('#c-points', root); p.classList.toggle('hidden'); p.classList.toggle('flex'); paintPoints(); });
  for (const b of $$('[data-pts]', root)) b.addEventListener('click', () => { $$('[data-pts]', root).forEach((x) => x.classList.toggle('active', x === b)); paintPoints(b.dataset.pts); });
  $('#c-new-from-outdated', root).addEventListener('click', () => $('#new-chat').click());
  $('#c-title', root).addEventListener('dblclick', async () => {
    if (!data) return;
    const t = prompt('Rename chat', data.chat.title);
    if (t && t.trim()) { await api('PATCH', `/api/chats/${data.chat.chat_no}`, { title: t.trim() }); $('#c-title', root).textContent = t.trim(); C.refreshChats(); }
  });
}

async function paintPoints(mode) {
  if (!root) return;
  const list = $('#c-points-list', root);
  if (!list || $('#c-points', root).classList.contains('hidden')) return;
  const which = mode || ($('[data-pts].active', root)?.dataset.pts) || 'chat';
  if (which === 'all') {
    const r = await api('GET', `/api/points?section=${C.section}`);
    list.innerHTML = r.chats.length ? r.chats.map((c) => `<div class="card !p-3"><a class="ref-link" data-ref="chat#${c.chat_no}">chat#${c.chat_no}</a> <span class="text-xs ${c.outdated ? 'text-rose-500' : 'text-slate-500'}">${esc(c.title)}</span>
      <ul class="mt-2 space-y-1">${c.points.map((p) => `<li class="text-[13px]">• ${esc(p.text)} <a class="ref-link" data-ref="${esc(p.ref)}">${esc(p.ref)}</a></li>`).join('')}</ul></div>`).join('') : '<p class="text-xs text-slate-500 text-center py-8">No main points yet</p>';
    return;
  }
  const pts = data ? data.main_points : [];
  list.innerHTML = pts.length ? pts.map((p) => `<div class="card !p-3 text-[13px]">${esc(p.text)}<div class="mt-1"><a class="ref-link" data-ref="${esc(p.ref)}">${esc(p.ref)}</a></div></div>`).join('')
    : '<p class="text-xs text-slate-500 text-center py-8">Main points appear here after each reply.</p>';
}

async function send() {
  const input = $('#c-input', root);
  const text = input.value.trim();
  if (!text) return;
  if (runningRequest()) { toast('Still generating - wait or press stop.', 'warn'); return; }
  const btn = $('#c-send', root);
  btn.disabled = true;
  try {
    if (!current[C.section]) {
      const r = await api('POST', '/api/chats', { section: C.section });
      current[C.section] = r.chat.chat_no;
      C.saveUi(`chat_${C.section}`, r.chat.chat_no);
      data = { chat: r.chat, messages: [], main_points: [], partial: '' };
      $('#c-msgs', root).innerHTML = '';
      $('#c-title', root).textContent = text.slice(0, 60);
      $('#c-ref', root).textContent = r.chat.ref;
    }
    const rid = uuid();
    const atts = pendingAtts.splice(0);
    paintAtts();
    const m = { chatno: null, prompt: text, reply: '', status: 'generating', request_id: rid, created_at: new Date().toISOString(), cards: [], attachments: atts };
    data.messages.push(m);
    streams.set(rid, { chat_no: data.chat.chat_no, msg_no: null, buf: '', status: 'generating', stage: 'thinking' });
    const box = $('#c-msgs', root);
    if (data.messages.length === 1) box.innerHTML = '';
    box.appendChild(messageEl(m));
    box.scrollTop = box.scrollHeight;
    input.value = ''; autosize();
    updateStopButton();
    const loc = shareLoc ? await getLocation() : null;
    const body = { text, request_id: rid, location: loc, attachments: atts.map((a) => a.id) };
    const prov = $('#c-provider', root);
    if (prov) body.provider = prov.value;
    const r = await api('POST', `/api/chats/${data.chat.chat_no}/messages`, body, { idempotencyKey: rid });
    m.chatno = r.msg_no;
    const wrap = root.querySelector(`[data-rid="${CSS.escape(rid)}"]`);
    if (wrap) {
      wrap.id = `msg-${data.chat.chat_no}-${r.msg_no}`;
      const refEl = wrap.querySelector('.flex.justify-end.gap-2');
      if (refEl && !refEl.querySelector('.ref-link')) refEl.insertAdjacentHTML('afterbegin', `<a class="ref-link" data-ref="chat#${data.chat.chat_no}.${r.msg_no}">chat#${data.chat.chat_no}.${r.msg_no}</a>`);
    }
    C.refreshChats();
  } catch (e) {
    toast(e.message, e.status === 409 ? 'warn' : 'error');
    const last = data && data.messages[data.messages.length - 1];
    if (last && last.status === 'generating' && !last.chatno) {
      last.status = 'error'; last.reply = `_${e.message}_`;
      const s = streams.get(last.request_id); if (s) s.status = 'error';
      paintStream(last.request_id);
    }
  } finally {
    btn.disabled = false;
    updateStopButton();
  }
}

let rec = null;
function dictate() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const btn = $('#c-mic', root);
  if (!SR) { toast('Speech-to-text needs Chrome, Edge or Safari.', 'warn'); return; }
  if (!window.isSecureContext) { toast('The microphone only works on the HTTPS address or localhost.', 'warn'); return; }
  if (rec) { rec.stop(); return; }
  const input = $('#c-input', root);
  const base = input.value ? input.value.trimEnd() + ' ' : '';
  rec = new SR();
  rec.lang = navigator.language || 'en-US';
  rec.interimResults = true;
  rec.continuous = false;
  rec.onresult = (e) => {
    let t = '';
    for (let i = 0; i < e.results.length; i++) t += e.results[i][0].transcript;
    input.value = base + t; autosize();
  };
  rec.onerror = (e) => toast(`Microphone: ${e.error}`, 'warn');
  rec.onend = () => { rec = null; btn.classList.remove('text-rose-500', 'animate-pulse'); input.focus(); };
  btn.classList.add('text-rose-500', 'animate-pulse');
  rec.start();
}
