// Locust stress-test tab: several runs can execute in parallel; each is a
// cancellable background job (cancel = exact request id over Socket.IO).
import { $, $$, api, cancelJob, esc, fmtRel, on, toast } from '../../core.js';
import { store } from './shared.js';

let root = null; const unsubs = []; const live = new Map(); // run_id -> latest stats

export const stressTab = {
  id: 'stress',
  label: 'Stress test',
  async mount(el) {
    root = el;
    const pre = store.stressPrefill || {};
    store.stressPrefill = null;
    el.innerHTML = `<div class="h-full overflow-y-auto scroll-thin p-5 md:p-8 space-y-5">
      <div><h2 class="text-xl font-semibold">Website stress test (Locust)</h2>
        <p class="text-sm text-slate-500">Only test systems you own or are explicitly authorised to test. Runs are capped at 200 users / 10 minutes and can be stopped any time.</p></div>
      <form id="ls-form" class="panel p-5 grid md:grid-cols-6 gap-3 items-end">
        <div class="md:col-span-3"><label class="label">Target base URL</label><input class="input font-mono" name="target_url" required placeholder="https://staging.mysite.dev" value="${esc(pre.url || '')}"></div>
        <div><label class="label">Users</label><input class="input" name="users" type="number" min="1" max="200" value="${Number(pre.users) || 20}"></div>
        <div><label class="label">Spawn /s</label><input class="input" name="spawn_rate" type="number" min="0.1" max="50" step="0.1" value="5"></div>
        <div><label class="label">Seconds</label><input class="input" name="duration" type="number" min="5" max="600" value="${Number(pre.duration) || 60}"></div>
        <div class="md:col-span-4"><label class="label">Paths (one per line)</label><textarea class="textarea font-mono text-xs" name="paths" rows="3">${esc((pre.paths || ['/']).join('\n'))}</textarea></div>
        <div class="md:col-span-2 space-y-2"><label class="label">Method</label><select class="select" name="method"><option>GET</option><option>HEAD</option></select></div>
        <label class="md:col-span-4 flex items-start gap-2 text-sm"><input type="checkbox" name="authorized" class="mt-1">
          <span>I own this target or have written permission to load-test it. I accept responsibility for this test.</span></label>
        <div class="md:col-span-2 flex justify-end"><button class="btn-primary">Start stress test</button></div>
      </form>
      <div id="ls-live" class="grid lg:grid-cols-2 gap-4"></div>
      <div class="panel p-5"><div class="flex items-center mb-3"><h3 class="font-semibold">Runs</h3><span class="flex-1"></span><button class="btn-plain btn-sm" id="ls-refresh">Refresh</button></div>
        <div id="ls-history" class="text-sm">Loading…</div></div>
      <div id="ls-detail"></div>
    </div>`;
    $('#ls-form', el).addEventListener('submit', start);
    $('#ls-refresh', el).addEventListener('click', loadRuns);
    unsubs.push(on('locust:stats', (d) => { if (!root) return; live.set(d.run_id, d); paintLive(); if (d.done) loadRuns(); }));
    unsubs.push(on('job', (j) => { if (root && j.kind === 'locust' && !['running', 'scheduled'].includes(j.status)) { setTimeout(loadRuns, 800); } }));
    await loadRuns();
  },
  unmount() { while (unsubs.length) unsubs.pop()(); root = null; },
};

async function start(e) {
  e.preventDefault();
  const f = new FormData(e.target);
  if (!f.get('authorized')) { toast('Please confirm you are authorised to test this target.', 'warn'); return; }
  const body = {
    target_url: String(f.get('target_url') || '').trim(), users: Number(f.get('users')), spawn_rate: Number(f.get('spawn_rate')),
    duration: Number(f.get('duration')), method: f.get('method'), authorized: true,
    paths: String(f.get('paths') || '/').split('\n').map((s) => s.trim()).filter(Boolean),
  };
  try {
    const r = await api('POST', '/api/coding/locust', body);
    live.set(r.run_id, { run_id: r.run_id, request_id: r.request_id, totals: {}, endpoints: [], elapsed: 0, duration: body.duration, target: body.target_url });
    paintLive();
    toast(`Stress test #${r.run_id} started`, 'ok');
    loadRuns();
  } catch (ex) { toast(ex.message, 'error', 6000); }
}

function stat(label, value) { return `<div class="card !p-2.5"><div class="text-[10px] uppercase tracking-wide text-slate-500">${label}</div><div class="text-lg font-semibold">${value}</div></div>`; }

function paintLive() {
  const box = root && $('#ls-live', root);
  if (!box) return;
  const items = [...live.values()].filter((d) => !d.done).sort((a, b) => b.run_id - a.run_id);
  box.innerHTML = items.map((d) => {
    const t = d.totals || {};
    const pct = Math.min(100, Math.round(100 * (d.elapsed || 0) / Math.max(d.duration || 1, 1)));
    return `<div class="panel p-4"><div class="flex items-center gap-2"><b>Run #${d.run_id}</b><span class="badge-blue">running</span>
      <span class="text-xs text-slate-500">${Math.round(d.elapsed || 0)}s / ${d.duration}s</span><span class="flex-1"></span>
      <button class="btn-danger btn-sm" data-stop="${esc(d.request_id)}">Stop</button></div>
      <div class="mt-2 h-1.5 rounded-full bg-slate-900/10 dark:bg-white/10 overflow-hidden"><div class="h-full bg-accent-500" data-w="${pct}"></div></div>
      <div class="mt-3 grid grid-cols-3 sm:grid-cols-6 gap-2">${stat('Requests', t.requests ?? 0)}${stat('Req/s', t.rps ?? 0)}${stat('Failures', t.failures ?? 0)}
        ${stat('Median', (t.median_ms ?? 0) + ' ms')}${stat('p95', (t.p95_ms ?? 0) + ' ms')}${stat('Max', (t.max_ms ?? 0) + ' ms')}</div></div>`;
  }).join('');
  for (const b of $$('[data-w]', box)) b.style.width = `${Math.max(3, Number(b.dataset.w))}%`;
  for (const b of $$('[data-stop]', box)) b.addEventListener('click', async () => { b.disabled = true; const ack = await cancelJob(b.dataset.stop); toast(ack.ok ? 'Stress test stopped' : ack.message, ack.ok ? 'ok' : 'warn'); });
}

async function loadRuns() {
  if (!root) return;
  const r = await api('GET', '/api/coding/locust');
  for (const run of r.runs) {
    if (run.status === 'running' && run.request_id && !live.has(run.id)) live.set(run.id, { run_id: run.id, request_id: run.request_id, totals: run.totals, endpoints: [], elapsed: 0, duration: run.duration });
    if (run.status !== 'running' && live.has(run.id)) live.get(run.id).done = true;
  }
  paintLive();
  const color = { completed: 'badge-green', running: 'badge-blue', cancelled: 'badge-gray', failed: 'badge-red' };
  $('#ls-history', root).innerHTML = r.runs.length ? `<div class="overflow-x-auto"><table class="table-glass"><thead><tr><th>#</th><th>Target</th><th>Users</th><th>Duration</th><th>Status</th><th>Requests</th><th>Fail %</th><th>Median</th><th>p95</th><th>Req/s</th><th></th></tr></thead><tbody>
    ${r.runs.map((x) => { const t = x.totals || {}; return `<tr><td>${x.id}</td><td class="font-mono text-xs max-w-[220px] truncate" title="${esc(x.target_url)}">${esc(x.target_url)}</td><td>${x.users}</td><td>${x.duration}s</td>
      <td><span class="${color[x.status] || 'badge-gray'}">${esc(x.status)}</span><div class="text-[10px] text-slate-500">${esc(fmtRel(x.created_at))}</div></td>
      <td>${t.requests ?? '-'}</td><td>${t.failure_pct ?? '-'}</td><td>${t.median_ms ?? '-'} ms</td><td>${t.p95_ms ?? '-'} ms</td><td>${t.rps ?? '-'}</td>
      <td><button class="btn-plain btn-sm" data-run="${x.id}">Details</button></td></tr>`; }).join('')}</tbody></table></div>`
    : '<p class="text-slate-500">No stress tests yet.</p>';
  for (const b of $$('[data-run]', root)) b.addEventListener('click', () => detail(Number(b.dataset.run)));
}

async function detail(id) {
  const r = await api('GET', `/api/coding/locust/${id}`);
  const s = r.stats || {};
  const rows = (s.endpoints || []).map((e) => `<tr><td class="font-mono text-xs">${esc(e.type)} ${esc(e.name)}</td><td>${e.requests}</td><td>${e.failures}</td><td>${e.median_ms}</td><td>${e.p95_ms}</td><td>${e.p99_ms}</td><td>${e.max_ms}</td><td>${e.rps}</td></tr>`).join('');
  const fails = (s.failures_detail || []).map((f) => `<li><span class="font-mono text-xs">${esc(f.method)} ${esc(f.name)}</span> ×${f.occurrences}: ${esc(f.error)}</li>`).join('');
  $('#ls-detail', root).innerHTML = `<div class="panel p-5 animate-fade-up"><h3 class="font-semibold">Run #${r.id} · ${esc(r.target_url)} · ${esc(r.status)}</h3>
    <div class="mt-3 overflow-x-auto"><table class="table-glass"><thead><tr><th>Endpoint</th><th>Requests</th><th>Failures</th><th>Median ms</th><th>p95</th><th>p99</th><th>Max</th><th>Req/s</th></tr></thead><tbody>${rows || '<tr><td colspan="8" class="text-slate-500">No data</td></tr>'}</tbody></table></div>
    ${fails ? `<div class="mt-4 text-sm"><div class="font-medium mb-1">Failures</div><ul class="list-disc pl-5 space-y-1">${fails}</ul></div>` : ''}</div>`;
  $('#ls-detail', root).scrollIntoView({ behavior: 'smooth', block: 'start' });
}
