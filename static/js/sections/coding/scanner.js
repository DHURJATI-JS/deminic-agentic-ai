// Vulnerability scanner tab: OSV.dev scans of pasted manifests, workspace files or whole cloned repos,
// each as a cancellable background job; results + history.
import { $, $$, api, cancelJob, esc, fmtRel, on, toast } from '../../core.js';
import { findingsTable, store, verdictBadge } from './shared.js';

let root = null; const unsubs = []; let running = new Set();

export const scannerTab = {
  id: 'scanner',
  label: 'Scanner',
  async mount(el) {
    root = el;
    el.innerHTML = `<div class="h-full overflow-y-auto scroll-thin p-5 md:p-8 space-y-5">
      <div><h2 class="text-xl font-semibold">Vulnerability scanner</h2>
        <p class="text-sm text-slate-500">OSV.dev checks PyPI &amp; npm dependencies; unpinned packages are resolved to the version that would be installed. Scans run in the background - switch tabs freely.</p></div>
      <div class="panel p-5">
        <div class="seg" role="tablist"><button class="seg-item active" data-mode="text">Paste</button><button class="seg-item" data-mode="file">Workspace file</button><button class="seg-item" data-mode="repo">Cloned repo</button></div>
        <form id="sc-form" class="mt-4 space-y-3">
          <div data-pane="text"><div class="flex gap-2 mb-2"><select class="select !w-56" name="filename">
            ${['auto', 'requirements.txt', 'package.json', 'pyproject.toml', 'Pipfile', 'package-lock.json', 'poetry.lock'].map((n) => `<option>${n}</option>`).join('')}</select>
            <span class="hint self-center">or just list packages: <span class="font-mono">requests==2.19.0, lodash@4.17.15</span></span></div>
            <textarea class="textarea font-mono text-xs" name="text" rows="8" placeholder="flask==0.12&#10;requests>=2.0&#10;jinja2"></textarea></div>
          <div data-pane="file" class="hidden"><label class="label">Workspace path</label><input class="input font-mono" name="path" placeholder="scratch/requirements.txt or repos/myrepo/package.json"></div>
          <div data-pane="repo" class="hidden"><label class="label">Cloned repository</label><select class="select" name="repo_id"><option value="">Loading…</option></select></div>
          <div class="flex gap-2"><button class="btn-primary">Scan</button><span id="sc-status" class="text-sm text-slate-500 self-center"></span></div>
        </form>
      </div>
      <div id="sc-result"></div>
      <div class="panel p-5"><div class="flex items-center mb-3"><h3 class="font-semibold">History</h3><span class="flex-1"></span><button class="btn-plain btn-sm" id="sc-refresh">Refresh</button></div>
        <div id="sc-history" class="text-sm">Loading…</div></div>
    </div>`;
    let mode = 'text';
    for (const b of $$('[data-mode]', el)) b.addEventListener('click', () => {
      mode = b.dataset.mode;
      $$('[data-mode]', el).forEach((x) => x.classList.toggle('active', x === b));
      $$('[data-pane]', el).forEach((p) => p.classList.toggle('hidden', p.dataset.pane !== mode));
    });
    $('#sc-form', el).addEventListener('submit', async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      const body = { mode, filename: f.get('filename'), text: f.get('text'), path: f.get('path'), repo_id: f.get('repo_id') ? Number(f.get('repo_id')) : null };
      try {
        const r = await api('POST', '/api/coding/scan', body);
        running.add(r.request_id);
        $('#sc-status', el).innerHTML = `scanning… <button class="btn-plain btn-sm text-rose-600" data-cancel="${esc(r.request_id)}">cancel</button>`;
        $('[data-cancel]', el).addEventListener('click', async (ev) => { const ack = await cancelJob(ev.target.dataset.cancel); toast(ack.ok ? 'Scan cancelled' : ack.message, ack.ok ? 'ok' : 'warn'); });
      } catch (ex) { toast(ex.message, 'error'); }
    });
    $('#sc-refresh', el).addEventListener('click', loadHistory);
    unsubs.push(on('scan:result', async (d) => {
      if (!root) return;
      running.delete(d.request_id);
      $('#sc-status', root).textContent = '';
      await showScan(d.scan_id);
      loadHistory();
    }));
    unsubs.push(on('job', (j) => {
      if (!root || j.kind !== 'vuln_scan' || !running.has(j.request_id)) return;
      if (j.status === 'running') $('#sc-status', root).firstChild && ($('#sc-status', root).firstChild.textContent = `${Math.round((j.progress || 0) * 100)}% · ${j.detail || ''} `);
      else if (j.status !== 'completed') { running.delete(j.request_id); $('#sc-status', root).textContent = `${j.status}: ${j.detail || ''}`; }
    }));
    loadRepos();
    loadHistory();
    if (store.scanPrefill) {
      $('textarea[name=text]', el).value = store.scanPrefill;
      store.scanPrefill = null;
    }
  },
  unmount() { while (unsubs.length) unsubs.pop()(); root = null; running = new Set(); },
};

async function loadRepos() {
  try {
    const r = await api('GET', '/api/coding/github/repos');
    const cloned = r.repos.filter((x) => x.cloned_path);
    $('select[name=repo_id]', root).innerHTML = cloned.length ? cloned.map((x) => `<option value="${x.id}">${esc(x.full_name)} (${esc(x.cloned_path)})</option>`).join('')
      : '<option value="">No cloned repositories yet</option>';
  } catch { /* ignore */ }
}

async function loadHistory() {
  if (!root) return;
  const r = await api('GET', '/api/coding/scans?limit=40');
  const box = $('#sc-history', root);
  box.innerHTML = r.scans.length ? `<div class="overflow-x-auto"><table class="table-glass"><thead><tr><th>When</th><th>Kind</th><th>Target</th><th>Verdict</th><th>Vulnerable</th><th></th></tr></thead><tbody>
    ${r.scans.map((s) => `<tr><td class="whitespace-nowrap">${esc(fmtRel(s.created_at))}</td><td><span class="chip">${esc(s.kind)}</span></td>
      <td class="font-mono text-xs max-w-[260px] truncate" title="${esc(s.target)}">${esc(s.target)}</td><td>${verdictBadge(s.verdict)}</td>
      <td>${s.vulnerable}/${s.checked}</td><td><button class="btn-plain btn-sm" data-scan="${s.id}">View</button></td></tr>`).join('')}</tbody></table></div>`
    : '<p class="text-slate-500">No scans yet.</p>';
  for (const b of $$('[data-scan]', box)) b.addEventListener('click', () => showScan(Number(b.dataset.scan)));
}

async function showScan(id) {
  if (!root) return;
  const s = await api('GET', `/api/coding/scans/${id}`);
  const res = s.result || {};
  const unknown = (res.unknown || []).map((u) => `<span class="badge-amber">${esc(u.name)}: not on ${esc(u.ecosystem)}</span>`).join(' ');
  const recs = (res.findings || []).map((f) => `<li><b>${esc(f.name)} ${esc(f.version)}</b>: ${esc(f.recommendation)}</li>`).join('');
  $('#sc-result', root).innerHTML = `<div class="panel p-5 animate-fade-up">
    <div class="flex flex-wrap items-center gap-2"><h3 class="font-semibold">Scan #${s.id}</h3>${verdictBadge(s.verdict)}<span class="chip">${esc(s.kind)}</span>
      <span class="font-mono text-xs text-slate-500">${esc(s.target)}</span><span class="ml-auto text-sm">${esc(res.message || '')}</span></div>
    <div class="mt-4">${findingsTable(res.findings || [])}</div>
    ${recs ? `<div class="mt-4 text-sm"><div class="font-medium mb-1">Recommended fixes / alternatives</div><ul class="list-disc pl-5 space-y-1">${recs}</ul></div>` : ''}
    ${unknown ? `<div class="mt-3 flex flex-wrap gap-1.5">${unknown}</div>` : ''}
    <details class="mt-3 text-xs text-slate-500"><summary class="cursor-pointer">${(res.checked || []).length} package(s) checked</summary>
      <div class="mt-2 font-mono">${(res.checked || []).map((c) => `${esc(c.ecosystem)}:${esc(c.name)}@${esc(c.version)}`).join(' · ')}</div></details></div>`;
}
