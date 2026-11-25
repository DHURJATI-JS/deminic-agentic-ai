// "Apply to workspace" buttons under AI code blocks that name a file
// (```python path=src/app.py). Single file -> /files/save (gated);
// several -> /apply = ONE protected job with a restore point (all or nothing).
import { api, esc, modal, on, registerEnhancer, toast } from '../../core.js';
import { icon } from '../../icons.js';
import { gateModal, reportHtml, store } from './shared.js';

const PATH_RE = /(?:^|\s)(?:path|file|filename)\s*[=:]\s*["']?([^\s"'`]+)/i;
const pendingApply = new Map(); // request_id -> paths

function parseFences(raw) {
  const blocks = [];
  const lines = String(raw || '').split('\n');
  let i = 0;
  while (i < lines.length) {
    const m = lines[i].match(/^ {0,3}(`{3,}|~{3,})\s*(.*)$/);
    if (!m) { i++; continue; }
    const fence = m[1]; const info = m[2] || '';
    const body = [];
    let closed = false;
    i++;
    while (i < lines.length) {
      const c = lines[i].match(/^ {0,3}(`{3,}|~{3,})\s*$/);
      if (c && c[1][0] === fence[0] && c[1].length >= fence.length) { closed = true; i++; break; }
      body.push(lines[i]); i++;
    }
    let pm = info.match(PATH_RE);
    if (!pm && body.length) { // tolerate "path=src/x.py" / "# file: src/x.py" as the first line of the block
      const first = body[0].match(/^\s*(?:#|\/\/|<!--|--)?\s*(?:path|file|filename)\s*[=:]\s*["'`]?([^\s"'`]+?)["'`]?\s*(?:-->)?\s*$/i);
      if (first) { pm = first; body.shift(); }
    }
    blocks.push({ info, lang: (info.split(/\s+/)[0] || '').toLowerCase(), path: pm ? pm[1] : '', content: body.join('\n') + (body.length ? '\n' : ''), closed });
  }
  return blocks;
}

// Which coding chat does this AI message belong to? (wrapper id = msg-<chat_no>-<msg_no>)
function chatNoFor(el) {
  const wrap = el && el.closest ? el.closest('[id^="msg-"]') : null;
  const m = wrap && wrap.id.match(/^msg-(\d+)-/);
  if (m) return Number(m[1]);
  const n = window.deminic && window.deminic.ui ? Number(window.deminic.ui('chat_coding')) : 0;
  return n || null;
}

// Relative paths go into that chat's project folder; explicit workspace paths are kept.
function targetPath(p, chatNo) {
  p = String(p || '').replace(/\\/g, '/').trim();
  while (p.startsWith('./')) p = p.slice(2);
  p = p.replace(/^\/+/, '');
  if (/^(scratch|repos|projects)\//.test(p)) return p;
  return chatNo ? `projects/chat-${chatNo}/${p}` : `scratch/${p}`;
}

async function confirmTargets(blocks, chatNo) {
  const body = document.createElement('div');
  body.innerHTML = `<p class="text-xs text-slate-500">Each file is checked by the security gate (OSV.dev dependencies, ClamAV, secrets) before anything is written.${blocks.length > 1 ? ' All files are applied together or not at all (restore point).' : ''}
    ${chatNo ? `Relative paths are saved in this chat's project (<span class="font-mono">projects/chat-${chatNo}/</span>); start a path with scratch/ or repos/ to target the unified workspace.` : ''}</p>
    <div class="mt-3 space-y-2">${blocks.map((b, i) => `<div><label class="label">File ${i + 1}</label><input class="input font-mono text-xs" data-t="${i}" value="${esc(targetPath(b.path, chatNo))}"></div>`).join('')}</div>`;
  const ok = await modal({ title: blocks.length > 1 ? `Apply ${blocks.length} files` : 'Apply to workspace', body, wide: true,
    actions: [{ label: 'Cancel', value: false }, { label: 'Scan & apply', kind: 'primary', value: true }] });
  if (!ok) return null;
  return blocks.map((b, i) => ({ path: targetPath(body.querySelector(`[data-t="${i}"]`).value.trim(), chatNo), content: b.content }));
}

async function applyBlocks(blocks, btn, chatNo) {
  const files = await confirmTargets(blocks, chatNo);
  if (!files) return;
  btn.disabled = true;
  try {
    if (files.length === 1) {
      const r = await api('POST', '/api/coding/files/save', { path: files[0].path, content: files[0].content, force: true, source: 'ai_apply' });
      btn.textContent = '✓ Applied';
      toast(`${files[0].path}: ${r.report.message}`, 'ok', 6000);
      store.openFile = files[0].path;
      if ((r.report.warnings || []).length) gateModal(r.report, 'Applied with warnings');
    } else {
      const r = await api('POST', '/api/coding/apply', { files, chat_ref: chatNo ? `chat#${chatNo}` : null });
      pendingApply.set(r.request_id, { paths: r.paths, btn });
      btn.textContent = 'Scanning…';
    }
  } catch (e) {
    btn.disabled = false;
    if (e.status === 422 && e.data.report) gateModal(e.data.report); else toast(e.message, 'error', 7000);
  }
}

function addButton(pre, label, onClick) {
  const bar = document.createElement('div');
  bar.className = 'mt-1.5 mb-3 flex items-center gap-2';
  const b = document.createElement('button');
  b.className = 'btn-ghost btn-sm';
  b.innerHTML = `${icon('save', 'w-3.5 h-3.5')} <span>${esc(label)}</span>`;
  b.addEventListener('click', () => onClick(b));
  bar.appendChild(b);
  pre.insertAdjacentElement('afterend', bar);
  return bar;
}

export function installApplyEnhancer() {
  registerEnhancer((el, raw, section) => {
    if (section !== 'coding' || !raw || (!raw.includes('```') && !raw.includes('~~~'))) return;
    const blocks = parseFences(raw);
    const pres = [...el.querySelectorAll('pre')];
    if (!pres.length || pres.length !== blocks.length) return;
    const withPath = [];
    const chatNo = chatNoFor(el);
    blocks.forEach((b, i) => {
      if (!b.path || !b.closed) return;
      withPath.push(b);
      const bar = addButton(pres[i], `Apply → ${targetPath(b.path, chatNo)}`, (btn) => applyBlocks([b], btn, chatNo));
      const hint = document.createElement('span');
      hint.className = 'text-[10px] text-slate-500';
      hint.textContent = 'gated: OSV.dev + ClamAV';
      bar.appendChild(hint);
    });
    if (withPath.length > 1) {
      const wrap = document.createElement('div');
      wrap.className = 'mt-2';
      const b = document.createElement('button');
      b.className = 'btn-primary btn-sm';
      b.textContent = `Apply all ${withPath.length} files (one restore point)`;
      b.addEventListener('click', () => applyBlocks(withPath, b, chatNo));
      wrap.appendChild(b);
      el.appendChild(wrap);
    }
  });

  on('job', (j) => {
    if (j.kind !== 'code_apply' || !pendingApply.has(j.request_id) || ['running', 'scheduled'].includes(j.status)) return;
    const { paths, btn } = pendingApply.get(j.request_id);
    pendingApply.delete(j.request_id);
    if (j.status === 'completed') {
      if (btn) btn.textContent = `✓ Applied ${paths.length} files`;
      toast(`Applied ${paths.length} file(s) - all passed the gate`, 'ok', 6000);
      store.openFile = paths[0];
    } else {
      if (btn) { btn.disabled = false; btn.textContent = 'Blocked - nothing written'; }
      const g = (j.meta || {}).gate;
      const body = g ? reportHtml({ verdict: 'block', message: `${g.path}: ${g.message}`, findings: g.findings, warnings: g.warnings })
        : `<p>${esc(j.detail || 'The changes were rolled back.')}</p>`;
      modal({ title: 'Security gate blocked the change - everything was rolled back', body, wide: true });
    }
  });
}
