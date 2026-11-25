// GitHub tab: link / unlink, all repos with stats (stars, forks, watchers, clones, views ...), sync,
// clone (+ ClamAV scan), rescan, open in editor and push changes to a NEW branch only.
import { $, $$, api, confirmModal, esc, fmtRel, modal, on, toast } from '../../core.js';
import { icon } from '../../icons.js';
import { store } from './shared.js';

let root = null; let C = null; const unsubs = [];

export const githubTab = {
  id: 'github',
  label: 'GitHub',
  async mount(el, ctx) {
    root = el; C = ctx;
    el.innerHTML = '<div class="h-full overflow-y-auto scroll-thin p-5 md:p-8" id="gh-root"><p class="text-sm text-slate-500">Loading…</p></div>';
    unsubs.push(on('repo:changed', () => load()));
    unsubs.push(on('job', (j) => { if (root && ['github_sync', 'github_clone', 'github_scan'].includes(j.kind)) paintJob(j); }));
    await load();
  },
  unmount() { while (unsubs.length) unsubs.pop()(); root = null; },
};

function paintJob(j) {
  const box = $('#gh-job', root);
  if (!box) return;
  if (['running', 'scheduled'].includes(j.status)) {
    box.classList.remove('hidden');
    box.innerHTML = `<span class="dot-typing text-accent-500"><span></span><span></span><span></span></span> ${esc(j.title)} · ${Math.round((j.progress || 0) * 100)}% · ${esc(j.detail || '')}`;
  } else {
    box.innerHTML = `${esc(j.title)}: <b>${esc(j.status)}</b> ${esc(j.detail || '')}`;
    setTimeout(() => box && box.classList.add('hidden'), 6000);
    if (j.status !== 'completed') toast(`${j.title}: ${j.detail}`, 'warn', 7000);
  }
}

async function load() {
  if (!root) return;
  let r;
  try { r = await api('GET', '/api/coding/github/repos'); } catch (e) { $('#gh-root', root).innerHTML = `<p class="text-rose-600">${esc(e.message)}</p>`; return; }
  const host = $('#gh-root', root);
  const head = `<div class="flex flex-wrap items-center gap-3"><div><h2 class="text-xl font-semibold">GitHub</h2>
      <p class="text-sm text-slate-500">Read your repos, clone them (ClamAV-scanned) and push changes to <b>new branches only</b> - nothing is ever deleted or modified on GitHub.</p></div>
      <span class="flex-1"></span>${r.linked ? `<span class="badge-green">linked as ${esc(r.login)}</span><button class="btn-ghost btn-sm" id="gh-sync">${icon('refresh', 'w-3.5 h-3.5')} Sync</button><button class="btn-plain btn-sm text-rose-600" id="gh-unlink">${icon('log-out', 'w-3.5 h-3.5')} Unlink</button>` : ''}</div>
    ${r.git ? '' : '<div class="mt-3 card !bg-amber-500/10 text-sm">git is not installed on the server - cloning is unavailable.</div>'}
    <div id="gh-job" class="hidden mt-3 card text-sm"></div>`;
  if (!r.linked && !r.repos.length) {
    // label / [input + button on ONE row] / hint - so the button lines up with the input, not the hint
    host.innerHTML = `${head}<form id="gh-link" class="mt-5 panel p-5">
      <label class="label" for="gh-token">GitHub personal access token</label>
      <div class="flex flex-col sm:flex-row sm:items-center gap-3">
        <input class="input flex-1 min-w-0" id="gh-token" type="password" name="token" autocomplete="off" placeholder="github_pat_…" required>
        <button class="btn-primary shrink-0">${icon('link', 'w-4 h-4')}Link GitHub</button>
      </div>
      <p class="hint">Fine-grained token with Contents: read &amp; write (for new branches) and Metadata: read. Stored encrypted.</p></form>`;
    $('#gh-link', host).addEventListener('submit', async (e) => {
      e.preventDefault();
      try { const x = await api('POST', '/api/coding/github/link', { token: new FormData(e.target).get('token') }); toast(`Linked as ${x.login} - syncing repositories`, 'ok'); load(); }
      catch (ex) { toast(ex.message, 'error'); }
    });
    return;
  }
  const cards = r.repos.map((x) => {
    const status = { clean: 'badge-green', infected: 'badge-red', error: 'badge-amber', cloning: 'badge-blue', none: 'badge-gray' }[x.clone_status] || 'badge-gray';
    const stat = (name, v, title) => `<span class="chip" title="${title}">${name.length > 2 ? icon(name, 'w-3 h-3') : name} ${v ?? '-'}</span>`;
    const pushed = (x.branches_pushed || []).slice(-3).map((b) => `<a class="text-xs" href="${esc(b.url)}" target="_blank" rel="noopener noreferrer">${esc(b.branch)}</a>`).join(' · ');
    return `<div class="panel p-4 flex flex-col gap-2" data-repo="${x.id}">
      <div class="flex items-start gap-2"><div class="min-w-0 flex-1"><a class="font-semibold truncate block" href="${esc(x.html_url)}" target="_blank" rel="noopener noreferrer">${esc(x.full_name)}</a>
        <div class="text-xs text-slate-500 line-clamp-2">${esc(x.description || 'No description')}</div></div>
        ${x.private ? '<span class="badge-gray">private</span>' : ''}${x.fork ? '<span class="badge-violet">fork</span>' : ''}</div>
      <div class="flex flex-wrap gap-1.5">${stat('★', x.stars, 'stars')}${stat('git-branch', x.forks, 'forks')}${stat('eye', x.watchers, 'watchers')}
        ${stat('alert', x.open_issues, 'open issues')}${stat('copy', x.clones != null ? `${x.clones} (${x.clones_uniques} unique)` : 'n/a', 'clones (14 days)')}
        ${stat('activity', x.views != null ? `${x.views}` : 'n/a', 'views (14 days)')}${x.language ? `<span class="chip">${icon('code', 'w-3 h-3')} ${esc(x.language)}</span>` : ''}</div>
      <div class="text-[11px] text-slate-500">pushed ${esc(fmtRel(x.pushed_at))} · ${x.size_kb} KB · default ${esc(x.default_branch)}${x.synced_at ? ` · synced ${esc(fmtRel(x.synced_at))}` : ''}</div>
      <div class="flex flex-wrap items-center gap-1.5 mt-auto pt-1"><span class="${status}">${esc(x.clone_status === 'none' ? 'not cloned' : x.clone_status)}</span>
        ${x.scan_report && x.scan_report.scanned != null ? `<span class="text-[10px] text-slate-500">${x.scan_report.scanned} files scanned${(x.scan_report.quarantined || []).length ? `, ${x.scan_report.quarantined.length} quarantined` : ''}</span>` : ''}
        <span class="flex-1"></span>
        ${x.cloned_path ? `<button class="btn-ghost btn-sm" data-open="${esc(x.cloned_path)}">${icon('code', 'w-3.5 h-3.5')} Open</button><button class="btn-plain btn-sm" data-rescan="${x.id}">${icon('shield-check', 'w-3.5 h-3.5')} Rescan</button>
          ${r.linked ? `<button class="btn-primary btn-sm" data-push="${x.id}">${icon('git-branch', 'w-3.5 h-3.5')} Push branch</button>` : ''}`
          : `<button class="btn-primary btn-sm" data-clone="${x.id}" ${x.job || !r.git ? 'disabled' : ''}>${x.job ? 'Cloning…' : 'Clone'}</button>`}</div>
      ${pushed ? `<div class="text-[11px]">Branches pushed: ${pushed}</div>` : ''}
      ${x.scan_report && x.scan_report.note ? `<div class="text-[11px] text-amber-600">${esc(x.scan_report.note)}</div>` : ''}</div>`;
  }).join('');
  host.innerHTML = `${head}<div class="mt-5 flex items-center gap-2"><input class="input !w-72" id="gh-filter" placeholder="Filter repositories"><span class="text-sm text-slate-500">${r.repos.length} repositories</span></div>
    <div class="mt-4 grid md:grid-cols-2 xl:grid-cols-3 gap-4" id="gh-grid">${cards || '<p class="text-slate-500 text-sm">No repositories yet - press Sync.</p>'}</div>`;
  $('#gh-sync', host)?.addEventListener('click', async () => { try { await api('POST', '/api/coding/github/sync'); toast('Sync started', 'ok'); } catch (e) { toast(e.message, 'warn'); } });
  $('#gh-unlink', host)?.addEventListener('click', async () => {
    if (!(await confirmModal('Unlink GitHub?', 'The token is deleted. Cloned repos stay in your workspace.', { confirmText: 'Unlink' }))) return;
    await api('DELETE', '/api/coding/github/link'); toast('GitHub unlinked', 'ok'); load();
  });
  $('#gh-filter', host).addEventListener('input', (e) => {
    const q = e.target.value.toLowerCase();
    for (const c of $$('[data-repo]', host)) c.classList.toggle('hidden', !c.textContent.toLowerCase().includes(q));
  });
  for (const b of $$('[data-clone]', host)) b.addEventListener('click', async () => {
    b.disabled = true;
    try { await api('POST', `/api/coding/github/repos/${b.dataset.clone}/clone`); toast('Cloning in the background - every file will be ClamAV-scanned', 'ok'); }
    catch (e) { b.disabled = false; toast(e.message, 'error'); }
  });
  for (const b of $$('[data-rescan]', host)) b.addEventListener('click', async () => { try { await api('POST', `/api/coding/github/repos/${b.dataset.rescan}/scan`); toast('ClamAV rescan started', 'ok'); } catch (e) { toast(e.message, 'warn'); } });
  for (const b of $$('[data-open]', host)) b.addEventListener('click', () => { store.openFile = null; C.saveUi('editor_file', ''); openFolder(b.dataset.open); });
  for (const b of $$('[data-push]', host)) b.addEventListener('click', () => pushDialog(Number(b.dataset.push)));
}

async function openFolder(path) {
  const r = await api('GET', `/api/coding/files/list?path=${encodeURIComponent(path)}`);
  const readme = r.entries.find((e) => e.type === 'file' && /^readme(\.|$)/i.test(e.name)) || r.entries.find((e) => e.type === 'file');
  store.openFile = readme ? readme.path : null;
  C.setTab('editor');
}

export async function pushDialog(repoId, preset = {}) {
  let r;
  try { r = await api('GET', `/api/coding/github/repos/${repoId}/changes`); } catch (e) { toast(e.message, 'error'); return; }
  const files = preset.files || r.files;
  const body = document.createElement('div');
  body.innerHTML = `<p class="text-xs text-slate-500">Changed files in <span class="font-mono">${esc(r.path)}</span>. They are pushed to a <b>new</b> branch on ${esc(r.repo)}; existing branches are never touched.</p>
    <div class="mt-3 grid gap-3"><div><label class="label">New branch name</label><input class="input font-mono" id="pb-branch" value="${esc(preset.branch || 'deminic/' + new Date().toISOString().slice(0, 10))}"></div>
    <div><label class="label">Commit message</label><input class="input" id="pb-msg" value="${esc(preset.message || 'Changes from Deminic')}"></div>
    <div class="max-h-60 overflow-y-auto scroll-thin card !p-2 space-y-1">${files.length ? files.map((f, i) => `<label class="flex items-center gap-2 text-xs"><input type="checkbox" checked data-f="${i}">
      <span class="font-mono truncate flex-1">${esc(f.path)}</span><span class="${f.status === 'new' ? 'badge-green' : 'badge-blue'}">${esc(f.status)}</span></label>`).join('') : '<p class="text-xs text-slate-500">No changes detected in the clone.</p>'}</div></div>`;
  const ok = await modal({ title: `Push to a new branch · ${r.repo}`, body, wide: true, actions: [{ label: 'Cancel', value: false }, { label: 'Push new branch', kind: 'primary', value: true }] });
  if (!ok) return;
  const paths = $$('[data-f]', body).filter((c) => c.checked).map((c) => files[Number(c.dataset.f)].path);
  if (!paths.length) { toast('No files selected', 'warn'); return; }
  try {
    const res = await api('POST', '/api/coding/github/push', { repo_id: repoId, branch: $('#pb-branch', body).value.trim(), message: $('#pb-msg', body).value.trim(), paths });
    await modal({ title: 'Branch pushed', body: `<p>${res.files.length} file(s) pushed to the new branch <b class="font-mono">${esc(res.branch)}</b>.</p>
      <p class="mt-2"><a href="${esc(res.branch_url)}" target="_blank" rel="noopener noreferrer">Open branch</a> · <a href="${esc(res.compare_url)}" target="_blank" rel="noopener noreferrer">Compare &amp; open a pull request</a></p>` });
    load();
  } catch (e) { toast(e.message, 'error', 8000); }
}
