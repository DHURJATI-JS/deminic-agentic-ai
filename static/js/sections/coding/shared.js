// Shared helpers for the coding section (findings tables, gate modal, cross-tab prefill).
import { esc, modal } from '../../core.js';

export const store = { stressPrefill: null, openFile: null, scanPrefill: null };

const SEV = { CRITICAL: 'badge-red', HIGH: 'badge-red', MEDIUM: 'badge-amber', MODERATE: 'badge-amber', LOW: 'badge-blue', UNKNOWN: 'badge-gray', NONE: 'badge-gray' };
export const sevBadge = (s) => `<span class="${SEV[(s || 'UNKNOWN').toUpperCase()] || 'badge-gray'}">${esc(s || 'unknown')}</span>`;
export const verdictBadge = (v) => v === 'go' ? '<span class="badge-green">GO</span>' : v === 'error' ? '<span class="badge-amber">SCANNER ERROR</span>' : '<span class="badge-red">BLOCKED</span>';

export function findingsTable(findings, { compact = false } = {}) {
  if (!findings || !findings.length) return '<p class="text-sm text-emerald-600 dark:text-emerald-400">No known vulnerabilities.</p>';
  const rows = findings.map((f) => {
    const ids = (f.vulns || []).slice(0, compact ? 2 : 4).map((v) => `<a href="${esc(v.url)}" target="_blank" rel="noopener noreferrer" class="font-mono text-[11px]">${esc(v.id)}</a>`).join(' ');
    const more = (f.count || 0) > (compact ? 2 : 4) ? ` <span class="text-[10px] text-slate-500">+${f.count - (compact ? 2 : 4)}</span>` : '';
    return `<tr><td><div class="font-medium">${esc(f.name)}</div><div class="text-[10px] text-slate-500">${esc(f.ecosystem)} · ${esc(f.source || '')}</div></td>
      <td class="font-mono text-xs">${esc(f.version || '')}</td><td>${sevBadge(f.max_severity)}<div class="text-[10px] text-slate-500 mt-0.5">${f.count} advisor${f.count === 1 ? 'y' : 'ies'}</div></td>
      <td class="max-w-[220px]">${ids}${more}${compact ? '' : `<div class="text-[11px] text-slate-500 mt-1">${esc((f.vulns[0] || {}).summary || '')}</div>`}</td>
      <td class="text-xs">${f.fix_version ? `<span class="badge-green">→ ${esc(f.fix_version)}</span>` : '<span class="badge-gray">no fix</span>'}
        ${f.alternative ? `<div class="mt-1 text-[11px]">alt: ${esc(f.alternative)}</div>` : ''}</td></tr>`;
  }).join('');
  return `<div class="overflow-x-auto"><table class="table-glass"><thead><tr><th>Package</th><th>Version</th><th>Severity</th><th>Advisories</th><th>Fix / alternative</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

export function reportHtml(report) {
  if (!report) return '';
  const warn = (report.warnings || []).map((w) => `<li>${esc(w)}</li>`).join('');
  const recs = (report.findings || []).map((f) => `<li><b>${esc(f.name)}</b>: ${esc(f.recommendation)}</li>`).join('');
  const clam = report.clamav ? `<span class="chip">ClamAV: ${esc(report.clamav.status)}${report.clamav.signature ? ' · ' + esc(report.clamav.signature) : ''}</span>` : '';
  return `<div class="space-y-3 text-sm">
    <div class="flex flex-wrap items-center gap-2">${verdictBadge(report.verdict)}<span>${esc(report.message || '')}</span></div>
    <div class="flex flex-wrap gap-1.5">${clam}<span class="chip">${(report.checked || []).length} dependency check(s)</span>
      ${report.secrets ? `<span class="badge-amber">${report.secrets} secret(s)</span>` : ''}<span class="chip">${report.duration_ms ?? '-'} ms</span></div>
    ${(report.findings || []).length ? findingsTable(report.findings) : ''}
    ${recs ? `<div><div class="font-medium mb-1">Recommended alternatives</div><ul class="list-disc pl-5 space-y-1">${recs}</ul></div>` : ''}
    ${warn ? `<div><div class="font-medium mb-1">Warnings</div><ul class="list-disc pl-5 space-y-1 text-amber-700 dark:text-amber-300">${warn}</ul></div>` : ''}
  </div>`;
}

export function gateModal(report, title = 'Security gate') {
  return modal({ title: `${title}: ${report.verdict === 'go' ? 'GO' : 'BLOCKED - nothing was written'}`, body: reportHtml(report), wide: true });
}

export function langOf(path) {
  const p = (path || '').toLowerCase();
  const name = p.split('/').pop();
  if (name === 'dockerfile' || name.startsWith('dockerfile.')) return 'dockerfile';
  const ext = name.includes('.') ? name.split('.').pop() : '';
  return {
    py: 'python', pyw: 'python', js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'jsx', ts: 'text/typescript',
    tsx: 'text/typescript-jsx', json: 'application/json', html: 'htmlmixed', htm: 'htmlmixed', xml: 'xml', svg: 'xml',
    css: 'css', scss: 'text/x-scss', less: 'text/x-less', md: 'markdown', markdown: 'markdown', c: 'text/x-csrc', h: 'text/x-csrc',
    cpp: 'text/x-c++src', cc: 'text/x-c++src', hpp: 'text/x-c++src', cxx: 'text/x-c++src', java: 'text/x-java', cs: 'text/x-csharp',
    kt: 'text/x-kotlin', go: 'go', rs: 'rust', php: 'php', rb: 'ruby', swift: 'swift', sh: 'shell', bash: 'shell', zsh: 'shell',
    yml: 'yaml', yaml: 'yaml', toml: 'toml', sql: 'sql', ini: 'properties', cfg: 'properties', conf: 'properties', env: 'properties',
    properties: 'properties', diff: 'diff', patch: 'diff', vue: 'htmlmixed', svelte: 'htmlmixed',
  }[ext] || null;
}

export function fmtBytes(n) {
  n = Number(n || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
