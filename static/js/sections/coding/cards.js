// Chat card renderers for coding tools.
import { el, esc, fmtRel } from '../../core.js';
import { cardShell } from '../../cards.js';
import { icon } from '../../icons.js';
import { pushDialog } from './github.js';
import { findingsTable, store, verdictBadge } from './shared.js';

function withButtons(html, handlers) {
  const node = el(html);
  for (const [sel, fn] of Object.entries(handlers)) node.querySelector(sel)?.addEventListener('click', fn);
  return node;
}

const ic = (name, cls = 'w-4 h-4 text-accent-500') => `<span class="inline-flex">${icon(name, cls)}</span>`;

export function registerCodingCards(registerCard) {
  registerCard('vuln_report', (c, ctx) => withButtons(cardShell(`
    <div class="flex flex-wrap items-center gap-2">${ic('shield-check')} <b>OSV.dev scan #${c.scan_id}</b>${verdictBadge(c.verdict)}
      <span class="text-xs text-slate-500">${c.checked} package(s) · ${esc(c.target || '')}</span>
      <button class="ml-auto btn-ghost btn-sm" data-open-scanner>${icon('external', 'w-3.5 h-3.5')} Open scanner</button></div>
    <div class="mt-2">${findingsTable(c.findings || [], { compact: true })}</div>
    ${(c.unknown || []).length ? `<div class="mt-2 text-xs text-amber-600">Not found on the registry: ${c.unknown.map(esc).join(', ')} - typo or hallucinated package?</div>` : ''}`),
  { '[data-open-scanner]': () => ctx.setTab('scanner') }));

  registerCard('file_open', (c, ctx) => withButtons(cardShell(`<div class="flex items-center gap-2">${ic('file')} Read <span class="font-mono text-xs">${esc(c.path)}</span>
    <span class="text-xs text-slate-500">${c.lines} lines</span><button class="ml-auto btn-ghost btn-sm" data-open>${icon('code', 'w-3.5 h-3.5')} Open in editor</button></div>`),
  { '[data-open]': () => { store.openFile = c.path; ctx.setTab('editor'); } }));

  registerCard('locust_confirm', (c, ctx) => withButtons(cardShell(`<div class="flex flex-wrap items-center gap-2">${ic('gauge')} <b>Stress test prepared</b> - not started
    <span class="font-mono text-xs">${esc(c.url || 'no URL yet')}</span><span class="chip">${c.users} users</span><span class="chip">${c.duration}s</span>
    <span class="chip">${(c.paths || []).map(esc).join(', ')}</span>
    <button class="ml-auto btn-primary btn-sm" data-go>${icon('play', 'w-3.5 h-3.5')} Review &amp; start</button></div>
    <p class="mt-1 text-xs text-slate-500">You must confirm that you own or are authorised to test the target.</p>`),
  { '[data-go]': () => { store.stressPrefill = { url: c.url, users: c.users, duration: c.duration, paths: c.paths }; ctx.setTab('stress'); } }));

  registerCard('github_repos', (c, ctx) => {
    if (!c.linked) return withButtons(cardShell(`<div class="flex items-center gap-2">${ic('github')} GitHub is not linked.<button class="btn-ghost btn-sm ml-auto" data-gh>${icon('link', 'w-3.5 h-3.5')} Link GitHub</button></div>`), { '[data-gh]': () => ctx.setTab('github') });
    const rows = (c.repos || []).map((r) => `<tr><td class="font-mono text-xs">${esc(r.full_name)}</td><td>★ ${r.stars}</td><td>${r.forks}</td><td>${r.clones ?? '-'}</td><td>${esc(r.clone_status)}</td></tr>`).join('');
    return withButtons(cardShell(`<div class="flex items-center gap-2">${ic('github')} <b>${esc(c.login)}</b>'s repositories<button class="ml-auto btn-ghost btn-sm" data-gh>${icon('external', 'w-3.5 h-3.5')} Open GitHub tab</button></div>
      <div class="mt-2 overflow-x-auto"><table class="table-glass"><thead><tr><th>Repo</th><th>Stars</th><th>Forks</th><th>Clones</th><th>Local</th></tr></thead><tbody>${rows}</tbody></table></div>`),
    { '[data-gh]': () => ctx.setTab('github') });
  });

  registerCard('repo_clone', (c, ctx) => withButtons(cardShell(`<div class="flex flex-wrap items-center gap-2">${ic('upload', 'w-4 h-4 text-accent-500 rotate-180')} <b>${esc(c.repo)}</b>
    <span class="chip">${esc(c.status || '')}</span>${c.path ? `<span class="font-mono text-xs">${esc(c.path)}</span>` : ''}
    <button class="ml-auto btn-ghost btn-sm" data-gh>${icon('github', 'w-3.5 h-3.5')} GitHub tab</button></div>
    <p class="mt-1 text-xs text-slate-500">Cloning runs in the background; every file is scanned by ClamAV and infected files are quarantined.</p>`),
  { '[data-gh]': () => ctx.setTab('github') }));

  registerCard('push_branch', (c) => withButtons(cardShell(`<div class="flex flex-wrap items-center gap-2">${ic('git-branch')} <b>New branch</b>
    <span class="font-mono text-xs">${esc(c.branch)}</span> on <span class="font-mono text-xs">${esc(c.repo)}</span>
    <button class="ml-auto btn-primary btn-sm" data-push>${icon('upload', 'w-3.5 h-3.5')} Review &amp; push</button></div>
    <div class="mt-1 text-xs text-slate-500">${(c.files || []).length} changed file(s): ${(c.files || []).slice(0, 6).map((f) => esc(f.path)).join(', ')}${(c.files || []).length > 6 ? '…' : ''}</div>
    <p class="mt-1 text-[11px] text-slate-500">Nothing is pushed until you confirm. Existing branches are never modified.</p>`),
  { '[data-push]': () => pushDialog(c.repo_id, { branch: c.branch, message: c.message }) }));

  registerCard('code_applied', (c) => cardShell(`<div class="flex items-center gap-2">${ic('check', 'w-4 h-4 text-emerald-500')} Applied ${(c.paths || []).map((p) => `<span class="font-mono text-xs">${esc(p)}</span>`).join(', ')} <span class="text-xs text-slate-500">${esc(fmtRel(c.at))}</span></div>`));
}
