// Settings page.
import { $, $$, api, bindThemeToggles, busy, confirmModal, esc, fmtDate, modal, on, socket, toast } from './core.js';

bindThemeToggles();
const root = $('#settings-root');
let S = null;

const section = (id, title, sub, inner) => `<section id="${id}" class="panel p-6 scroll-mt-6"><h2 class="text-lg font-semibold">${title}</h2>
  ${sub ? `<p class="text-sm text-slate-500 mt-0.5">${sub}</p>` : ''}<div class="mt-5">${inner}</div></section>`;
const field = (name, label, value = '', { type = 'text', hint = '', attrs = '' } = {}) => `<div><label class="label" for="f-${name}">${label}</label>
  <input class="input" id="f-${name}" name="${name}" type="${type}" value="${esc(value ?? '')}" ${attrs} autocomplete="off">${hint ? `<p class="hint">${hint}</p>` : ''}</div>`;
const keyField = (name, label, masked, hint = '') => field(name, label, '', { type: 'password', hint: `${masked ? `Saved: ${esc(masked)} · leave blank to keep, type __clear__ to remove. ` : 'Not set. '}${hint}` });

function formData(form) {
  const out = {};
  for (const [k, v] of new FormData(form).entries()) out[k] = v;
  for (const cb of $$('input[type=checkbox]', form)) out[cb.name] = cb.checked;
  return out;
}

async function load() {
  S = await api('GET', '/api/settings');
  const p = S.profile; const b = S.business; const pr = S.providers; const m = S.smtp; const g = S.github;
  root.innerHTML = [
    section('profile', 'Profile', 'Your name and username are separate - the AI uses your name; you sign in with your username or e-mail.', `
      <div class="flex items-center gap-4 mb-5">
        <img id="avatar-img" src="${esc(p.avatar_url || '/static/img/avatar.svg')}" alt="Profile photo" class="w-20 h-20 rounded-full object-cover bg-slate-200 dark:bg-slate-700">
        <div class="flex flex-wrap gap-2"><label class="btn-ghost cursor-pointer">Upload photo<input type="file" id="avatar-file" accept="image/*" class="sr-only"></label>
        ${p.avatar_url ? '<button class="btn-plain" id="avatar-remove">Remove</button>' : ''}</div>
        <p class="text-xs text-slate-500">Scanned by ClamAV, metadata stripped. Your AI can see it when you ask about it.</p></div>
      <form id="profile-form" class="grid md:grid-cols-2 gap-4">
        ${field('name', 'Name', p.name)}${field('username', 'Username', p.username)}${field('email', 'E-mail (password resets)', p.email, { type: 'email' })}
        ${field('ai_name', 'AI name', p.ai_name, { hint: 'Default: Deminic' })}
        <div class="md:col-span-2"><label class="label" for="f-about">About you</label><textarea class="textarea" id="f-about" name="about" rows="3">${esc(p.about)}</textarea></div>
        ${field('timezone', 'Time zone', p.timezone, { hint: 'IANA name, e.g. Asia/Kolkata' })}
        <div><label class="label">Theme</label><select class="select" name="theme">${['system', 'light', 'dark'].map((t) => `<option ${t === p.theme ? 'selected' : ''}>${t}</option>`).join('')}</select></div>
        <div class="md:col-span-2"><button class="btn-primary">Save profile</button></div></form>
      <form id="pw-form" class="mt-6 divider pt-5 grid md:grid-cols-3 gap-4 items-end">
        ${field('current_password', 'Current password', '', { type: 'password' })}${field('new_password', 'New password', '', { type: 'password' })}
        <button class="btn-ghost">Change password</button></form>`),
    section('business', 'Business', 'Renaming your business is instant everywhere (replies use a {business_name} variable). Changing what it does starts a new business context.', `
      <form id="biz-form" class="grid gap-4">
        <label class="flex items-center gap-2 text-sm"><input type="checkbox" name="owns_business" ${b.owns_business ? 'checked' : ''}> I own a business</label>
        ${field('business_name', 'Business name', b.business_name)}
        <div><label class="label" for="f-business_description">What does the business do?</label><textarea class="textarea" id="f-business_description" name="business_description" rows="4">${esc(b.business_description)}</textarea>
        <p class="hint">Business context version ${b.business_version}. Changing this wipes business AI summaries and flags old business chats in red.</p></div>
        <div><button class="btn-primary">Save business</button></div></form>`),
    section('providers', 'AI providers', 'Local gateway first, dockerised Gemma/Qwen fallback if it is down, online providers when a task is beyond the local model\'s limits.', `
      <div id="health" class="mb-5 grid sm:grid-cols-2 lg:grid-cols-3 gap-2 text-sm"><div class="card">Checking providers…</div></div>
      <form id="prov-form" class="grid md:grid-cols-2 gap-4">
        ${field('gateway_url', 'Gateway base URL', pr.gateway_url)}${keyField('gateway_key', 'Gateway key', pr.gateway_key)}
        ${field('local_model', 'Chat model (gateway)', pr.local_model)}${field('coder_model', 'Coding model (gateway, Qwen)', pr.coder_model, { hint: 'Gemma is never used in coding mode.' })}
        <label class="md:col-span-2 flex items-center gap-2 text-sm"><input type="checkbox" name="use_docker_fallback" ${pr.use_docker_fallback ? 'checked' : ''}> Use the local Docker fallback models (${esc(pr.fallback_chat_model)} / ${esc(pr.fallback_coder_model)}) when the gateway is down</label>
        ${keyField('claude_key', 'Claude API key', pr.claude_key)}${field('claude_model', 'Claude model', pr.claude_model, { hint: 'e.g. claude-opus-5-5, claude-sonnet-5-5, claude-haiku-5-5' })}
        ${keyField('openai_key', 'ChatGPT (OpenAI) key', pr.openai_key)}${field('openai_model', 'OpenAI model', pr.openai_model)}
        ${keyField('github_models_key', 'GitHub Models / Copilot token', pr.github_models_key)}${field('github_models_model', 'GitHub model', pr.github_models_model)}
        <div><label class="label">Escalation order</label><select class="select" name="provider_order">${[['claude', 'openai', 'github'], ['openai', 'claude', 'github'], ['github', 'claude', 'openai']].map((o) => `<option value="${o.join(',')}" ${o.join(',') === pr.provider_order.join(',') ? 'selected' : ''}>${o.join(' → ')}</option>`).join('')}</select></div>
        <div><label class="label">Coding model</label><select class="select" name="coding_provider">${[['auto', 'Qwen (local) → online fallback'], ['local', 'Local Qwen only'], ['claude', 'Claude'], ['openai', 'ChatGPT'], ['github', 'GitHub Models']].map(([v, l]) => `<option value="${v}" ${v === pr.coding_provider ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
        ${keyField('youtube_api_key', 'YouTube Data API key (optional)', pr.youtube_api_key, 'Improves finding movies / songs to play.')}
        ${keyField('tmdb_api_key', 'TMDB API key (optional)', pr.tmdb_api_key, 'Shows exactly where a movie streams (Netflix, Prime Video...).')}
        ${field('watch_region', 'Streaming region', pr.watch_region, { hint: 'Two-letter country code, e.g. IN, US' })}${field('news_region', 'News region', pr.news_region)}
        <label class="flex items-center gap-2 text-sm"><input type="checkbox" name="share_location" ${pr.share_location ? 'checked' : ''}> Allow location for tasks, weather and the travel guide</label>
        <div class="md:col-span-2 flex flex-wrap gap-2"><button class="btn-primary">Save providers</button>
          ${['gateway', 'coder', 'ollama', 'claude', 'openai', 'github'].map((x) => `<button type="button" class="btn-ghost btn-sm" data-test="${x}">Test ${x}</button>`).join('')}</div></form>`),
    section('smtp', 'E-mail (SMTP)', 'Family e-mails, cold sales mails and password-reset links are sent through your own mailbox.', `
      <form id="smtp-form" class="grid md:grid-cols-3 gap-4">
        <div class="md:col-span-2">${field('smtp_host', 'SMTP server', m.smtp_host)}</div>${field('smtp_port', 'Port', m.smtp_port, { type: 'number' })}
        <div><label class="label">Security</label><select class="select" name="smtp_security">${['starttls', 'ssl', 'none'].map((x) => `<option ${x === m.smtp_security ? 'selected' : ''}>${x}</option>`).join('')}</select></div>
        ${field('smtp_username', 'Username', m.smtp_username)}${keyField('smtp_password', 'SMTP key / app password', m.smtp_password)}
        <div class="md:col-span-3">${field('smtp_from_email', 'Preferred e-mail to send from', m.smtp_from_email, { type: 'email' })}</div>
        <div class="md:col-span-3 flex gap-2"><button class="btn-primary">Save SMTP</button><button type="button" class="btn-ghost" id="smtp-test">Send test e-mail</button></div></form>`),
    section('github', 'GitHub', 'Link a personal access token to list, sync and clone repos. The AI can only push NEW branches - it never deletes or modifies anything.', `
      <div id="gh-box">${g.linked ? `<div class="flex items-center gap-3"><span class="badge-green">linked</span><span>as <b>${esc(g.login)}</b></span><button class="btn-danger btn-sm ml-auto" id="gh-unlink">Unlink</button></div>`
        : `<form id="gh-form"><label class="label" for="f-gh_token">Personal access token</label>
          <div class="flex flex-col sm:flex-row sm:items-center gap-2"><input class="input flex-1 min-w-0" id="f-gh_token" name="gh_token" type="password" autocomplete="off" required>
          <button class="btn-primary shrink-0">Link GitHub</button></div>
          <p class="hint">Fine-grained token with repo read + contents write (for new branches).</p></form>`}</div>`),
    section('memory', 'Global memory', 'Each mode has its own memory. Deleting runs as a protected background task with a restore point.', `
      <div class="grid sm:grid-cols-3 gap-3">${['personal', 'business', 'coding'].map((s) => `<div class="card"><div class="font-medium capitalize">${s}</div>
        <div class="text-xs text-slate-500 mt-1" data-count="${s}">…</div><button class="btn-danger btn-sm mt-3" data-wipe="${s}">Delete ${s} memory</button>
        <div class="mt-2 text-xs" data-wipe-state="${s}"></div></div>`).join('')}</div>`),
    section('restore', 'Restore points', 'Protected tasks snapshot what they change first - like a Windows restore point. Interrupted tasks are rolled back automatically.', '<div id="rp-list" class="text-sm">Loading…</div>'),
    section('danger', 'Danger zone', 'Global reset deletes everything you have stored (chats, memories, tasks, clients, files, repos...). Your account and settings are kept.', `
      <button class="btn-danger" id="reset-btn">Global reset…</button><div id="reset-state" class="mt-3 text-sm"></div>`),
    section('about', 'About', '', `<div class="text-sm space-y-2"><p>Deminic agentic AI &middot; self-hosted.</p>
      <p>Developer: <a href="mailto:dhurjatisharma2010@gmail.com">dhurjatisharma2010@gmail.com</a> &middot; <a href="https://github.com/dhurjati-js" target="_blank" rel="noopener noreferrer">github.com/dhurjati-js</a></p>
      <p><a href="/disclaimer" class="disclaimer-tag no-underline">NO LIABILITY - read the disclaimer</a></p>
      <p>Other devices: install <a href="/ca.crt">the local certificate</a> once to use the HTTPS address (needed for microphone + location).</p></div>`),
  ].join('');
  bind();
  health();
  memoryCounts();
  restorePoints();
}

function bind() {
  $('#profile-form').addEventListener('submit', submit('PUT', '/api/settings/profile', 'Profile saved', true));
  $('#pw-form').addEventListener('submit', submit('PUT', '/api/settings/password', 'Password changed'));
  $('#prov-form').addEventListener('submit', submit('PUT', '/api/settings/providers', 'Providers saved', true, (d) => { d.provider_order = String(d.provider_order).split(','); return d; }));
  $('#smtp-form').addEventListener('submit', submit('PUT', '/api/settings/smtp', 'SMTP saved', true, (d) => { d.smtp_port = Number(d.smtp_port) || 587; return d; }));
  $('#biz-form').addEventListener('submit', saveBusiness);
  $('#avatar-file').addEventListener('change', async (e) => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append('avatar', f);
    try { const r = await api('POST', '/api/settings/avatar', fd); $('#avatar-img').src = r.avatar_url; toast('Photo updated', 'ok'); } catch (ex) { toast(ex.message, 'error'); }
  });
  $('#avatar-remove')?.addEventListener('click', async () => { await api('DELETE', '/api/settings/avatar'); load(); });
  for (const b of $$('[data-test]')) b.addEventListener('click', async () => {
    busy(b, true);
    try { const r = await api('POST', '/api/settings/providers/test', { provider: b.dataset.test }); toast(r.message, r.ok ? 'ok' : 'warn', 6000); } finally { busy(b, false); }
  });
  $('#smtp-test').addEventListener('click', async (e) => { busy(e.target, true); try { const r = await api('POST', '/api/settings/smtp/test'); toast(r.message, r.ok ? 'ok' : 'warn', 7000); } finally { busy(e.target, false); } });
  $('#gh-form')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    try { const r = await api('POST', '/api/coding/github/link', { token: $('#f-gh_token').value.trim() }); toast(`GitHub linked as ${r.login}`, 'ok'); load(); } catch (ex) { toast(ex.message, 'error'); }
  });
  $('#gh-unlink')?.addEventListener('click', async () => {
    if (!(await confirmModal('Unlink GitHub?', 'The token is deleted. Cloned repos stay in your workspace.', { confirmText: 'Unlink' }))) return;
    await api('DELETE', '/api/coding/github/link'); toast('GitHub unlinked', 'ok'); load();
  });
  for (const b of $$('[data-wipe]')) b.addEventListener('click', () => wipe(b.dataset.wipe));
  $('#reset-btn').addEventListener('click', reset);
}

function submit(method, url, okMsg, reload = false, transform = (d) => d) {
  return async (e) => {
    e.preventDefault();
    const btn = e.submitter || e.target.querySelector('button');
    busy(btn, true);
    try {
      const data = transform(formData(e.target));
      if (data.theme) { document.documentElement.dataset.theme = data.theme; }
      await api(method, url, data);
      toast(okMsg, 'ok');
      if (reload) await load();
    } catch (ex) { toast(ex.message, 'error', 6000); } finally { busy(btn, false); }
  };
}

async function saveBusiness(e) {
  e.preventDefault();
  const data = formData(e.target);
  try {
    await api('PUT', '/api/settings/business', data);
    toast('Business saved', 'ok'); load();
  } catch (ex) {
    if (ex.status === 409 && ex.data.code === 'confirm_new_business') {
      const ok = await modal({
        title: 'This is a new business',
        body: `<p class="text-rose-600 dark:text-rose-400 font-medium">${esc(ex.data.message)}</p><p class="mt-2">${ex.data.affected_chats} existing business chat(s) will be marked with a red warning. To avoid stale advice, don't continue old chats - start a new one.</p>`,
        actions: [{ label: 'Cancel', value: false }, { label: 'Yes, start a new business context', kind: 'danger', value: true }],
      });
      if (ok) { const r = await api('PUT', '/api/settings/business', { ...data, confirm_new_business: true }); toast(`Business context v${r.business.business_version}: ${r.summaries_wiped} summaries wiped, ${r.outdated_chats} chats flagged`, 'warn', 8000); load(); }
    } else toast(ex.message, 'error');
  }
}

async function health() {
  try {
    const h = await api('GET', '/api/settings/providers/health');
    $('#health').innerHTML = Object.entries(h).map(([k, v]) => `<div class="card !p-3"><div class="flex items-center gap-2"><span class="w-2 h-2 rounded-full ${v.ok === true ? 'bg-emerald-500' : v.ok === false ? 'bg-rose-500' : 'bg-slate-400'}"></span><b>${esc(v.label)}</b></div>
      <div class="text-[11px] text-slate-500 mt-1 truncate">${v.ok === null ? 'key saved (not pinged)' : v.ok ? esc((v.models || []).slice(0, 6).join(', ')) : 'unreachable'}</div></div>`).join('');
  } catch { /* ignore */ }
}

async function memoryCounts() {
  for (const s of ['personal', 'business', 'coding']) {
    api('GET', `/api/memory?section=${s}`).then((r) => { const el = $(`[data-count="${s}"]`); if (el) el.textContent = `${r.memories.length} memories${r.wipe_running ? ' · deletion running' : ''}`; });
  }
}

async function wipe(sec) {
  if (!(await confirmModal(`Delete ${sec} memory?`, 'Protected task: cannot be cancelled; rolled back automatically if interrupted. Tasks/reminders stay but are marked stale.', { danger: true, confirmText: 'Delete' }))) return;
  try { const r = await api('POST', '/api/memory/wipe', { section: sec }); $(`[data-wipe-state="${sec}"]`).textContent = `running (request ${r.request_id.slice(0, 8)})`; } catch (ex) { toast(ex.message, 'error'); }
}

async function restorePoints() {
  const r = await api('GET', '/api/restore-points');
  const color = { committed: 'badge-green', active: 'badge-blue', restored: 'badge-amber', restore_failed: 'badge-red' };
  $('#rp-list').innerHTML = r.restore_points.length ? `<table class="table-glass"><thead><tr><th>Created</th><th>Label</th><th>Changes</th><th>Status</th></tr></thead><tbody>
    ${r.restore_points.map((p) => `<tr><td>${esc(fmtDate(p.created_at))}</td><td>${esc(p.label)}</td><td>${p.changes}</td><td><span class="${color[p.status] || 'badge-gray'}">${esc(p.status)}</span>${p.restored_at ? `<div class="text-[10px] text-slate-500">restored ${esc(fmtDate(p.restored_at))}</div>` : ''}</td></tr>`).join('')}</tbody></table>`
    : '<p class="text-slate-500">No restore points yet.</p>';
}

async function reset() {
  const input = document.createElement('div');
  input.innerHTML = '<p>This permanently deletes all your data. Type <b>RESET</b> to confirm.</p><input class="input mt-3" id="reset-confirm" autocomplete="off">';
  const ok = await modal({ title: 'Global reset', body: input, actions: [{ label: 'Cancel', value: false }, { label: 'Delete everything', kind: 'danger', value: true }] });
  if (!ok) return;
  try { const r = await api('POST', '/api/settings/reset', { confirm: $('#reset-confirm').value.trim() }); $('#reset-state').textContent = `Reset running (request ${r.request_id.slice(0, 8)}) - protected by a restore point.`; } catch (ex) { toast(ex.message, 'error'); }
}

socket();
on('job', (j) => {
  if (j.kind === 'memory_wipe') {
    const el = $(`[data-wipe-state="${j.meta?.section}"]`);
    if (el) el.textContent = `${j.status} · ${Math.round((j.progress || 0) * 100)}% · ${j.detail || ''}`;
    if (!['running'].includes(j.status)) { memoryCounts(); restorePoints(); }
  }
  if (j.kind === 'global_reset') {
    $('#reset-state').textContent = `${j.status} · ${Math.round((j.progress || 0) * 100)}% · ${j.detail || ''}`;
    if (j.status === 'completed') toast('Everything was deleted', 'warn');
  }
});
for (const a of $$('[data-nav]')) a.addEventListener('click', () => $$('[data-nav]').forEach((x) => x.classList.toggle('bg-white/70', x === a)));
load().catch((e) => { root.innerHTML = `<div class="panel p-6 text-rose-600">${esc(e.message)}</div>`; });
