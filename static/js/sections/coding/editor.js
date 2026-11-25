// Coding editor tab: workspace tree (unified workspace OR this coding chat's
// project folder) + CodeMirror 5 (function colouring overlay, auto-closing
// brackets/quotes/tags, matching, folding, comments, undo/redo) + image/SVG
// viewer + version history + rename / copy / download / drag-and-drop upload.
// Every write goes through the server's security gate (OSV.dev + ClamAV + secrets).
import { $, $$, api, confirmModal, esc, fmtDate, fmtRel, modal, on, toast } from '../../core.js';
import { icon } from '../../icons.js';
import { fmtBytes, gateModal, langOf, store, verdictBadge } from './shared.js';

let root = null; let C = null; let cm = null; let cur = null;
const docs = new Map(); // path -> CodeMirror.Doc (keeps per-file undo history while mounted)
const children = new Map(); // dir path -> entries
const expanded = new Set(['scratch', 'repos', 'projects']);
let selectedDir = 'scratch';
let mode = 'unified'; // 'unified' | 'project'
let projectChat = null; let projectRoot = null;
const unsubs = [];
const ROOTS = ['scratch', 'repos', 'projects'];

const KEYWORDS = new Set(['if', 'for', 'while', 'switch', 'catch', 'return', 'typeof', 'function', 'def', 'class', 'elif',
  'with', 'sizeof', 'await', 'async', 'yield', 'not', 'and', 'or', 'in', 'is', 'lambda', 'import', 'from', 'except',
  'assert', 'delete', 'void', 'throw', 'foreach', 'match', 'when', 'fn', 'func', 'else', 'do', 'try', 'case', 'new']);

const fnOverlay = {
  name: 'fn-call',
  token(stream) {
    if (stream.match(/^[A-Za-z_$][\w$]*(?=\s*\()/)) {
      const word = stream.current();
      const before = stream.string.slice(0, stream.start);
      if (KEYWORDS.has(word) || /\b(def|function|class|fn|func|new|struct|interface)\s+$/.test(before)) return null;
      return 'fn-call';
    }
    if (stream.match(/^[A-Za-z_$][\w$]*/)) return null;
    stream.next();
    return null;
  },
};

const parentOf = (p) => p.split('/').slice(0, -1).join('/');
const nameOf = (p) => p.split('/').pop();
const isRoot = (p) => ROOTS.includes(p);
const isContainer = (p) => { const s = p.split('/'); return s.length === 1 || (['repos', 'projects'].includes(s[0]) && s.length === 2); };
const scopeRoot = () => (mode === 'project' && projectRoot ? projectRoot : '');
const btn = (act, ic, title, extra = '') => `<button class="btn-plain btn-icon btn-sm" data-act="${act}" title="${title}" aria-label="${title}" ${extra}>${icon(ic)}</button>`;

export const editorTab = {
  id: 'editor',
  label: 'Editor',
  async mount(el, ctx) {
    root = el; C = ctx;
    el.innerHTML = `<div class="h-full flex min-h-0">
      <aside id="ed-side" class="w-72 shrink-0 border-r border-slate-900/[0.06] dark:border-white/[0.07] flex-col min-h-0 hidden md:flex">
        <div class="p-2 border-b border-slate-900/[0.06] dark:border-white/[0.07] space-y-2">
          <div class="seg w-full grid grid-cols-2" role="tablist" aria-label="Workspace">
            <button class="seg-item justify-center" data-mode="unified" title="Every file in your workspace">${icon('layers', 'w-3.5 h-3.5')} Unified</button>
            <button class="seg-item justify-center truncate" data-mode="project" id="ed-mode-project" title="Files of the open coding chat">${icon('folder', 'w-3.5 h-3.5')} <span id="ed-project-label">Chat project</span></button>
          </div>
          <div class="flex items-center gap-0.5">
            <span class="text-[11px] font-semibold uppercase tracking-wide text-slate-500 pl-1 truncate" id="ed-scope">Workspace</span><span class="flex-1"></span>
            ${btn('new-file', 'file-plus', 'New file')}${btn('new-folder', 'folder-plus', 'New folder')}
            <label class="btn-plain btn-icon btn-sm cursor-pointer" title="Upload files (ClamAV scanned) - or drag &amp; drop onto the tree" aria-label="Upload">${icon('upload')}<input type="file" multiple class="sr-only" id="ed-upload"></label>
            ${btn('refresh', 'refresh', 'Refresh')}
          </div>
        </div>
        <div id="ed-tree" class="flex-1 overflow-y-auto scroll-thin p-1.5 text-[13px] select-none rounded-xl transition" role="tree"></div>
        <div class="px-3 py-2 text-[10px] text-slate-500 border-t border-slate-900/[0.06] dark:border-white/[0.07]">New files &amp; uploads go to <span id="ed-target" class="font-mono">scratch</span> · drop files on a folder to upload</div>
      </aside>
      <div class="flex-1 min-w-0 flex flex-col min-h-0">
        <div class="h-12 shrink-0 px-2 md:px-3 flex items-center gap-1 border-b border-slate-900/[0.06] dark:border-white/[0.07]">
          ${btn('toggle-tree', 'menu', 'Files', 'data-mobile')}
          <div class="min-w-0 flex-1"><div id="ed-path" class="font-mono text-xs truncate">No file open</div><div id="ed-meta" class="text-[10px] text-slate-500 truncate"></div></div>
          ${btn('undo', 'undo', 'Undo (Ctrl+Z)')}${btn('redo', 'redo', 'Redo (Ctrl+Y)')}${btn('history', 'history', 'Version history')}
          ${btn('rename-cur', 'pen', 'Rename file')}${btn('download-cur', 'upload', 'Download file', 'data-flip')}${btn('delete', 'trash', 'Delete file')}
          <button class="btn-primary btn-sm" data-act="save" title="Save (Ctrl+S) - scanned before writing">${icon('save')} <span class="hidden sm:inline">Save · gate</span></button>
        </div>
        <div id="ed-gate" class="hidden shrink-0 px-4 py-2 text-sm border-b border-slate-900/[0.06] dark:border-white/[0.07]"></div>
        <div id="ed-main" class="flex-1 min-h-0 relative"></div>
      </div>
      <aside id="ed-history" class="hidden w-72 shrink-0 border-l border-slate-900/[0.06] dark:border-white/[0.07] flex-col min-h-0">
        <div class="h-12 px-3 flex items-center border-b border-slate-900/[0.06] dark:border-white/[0.07]"><b class="text-sm">History</b><span class="flex-1"></span>${btn('history', 'x', 'Close history')}</div>
        <div id="ed-hist-list" class="flex-1 overflow-y-auto scroll-thin p-2 space-y-1.5 text-xs"></div>
      </aside>
    </div>`;
    for (const b of $$('[data-mobile]', el)) b.classList.add('md:hidden');
    for (const b of $$('[data-flip] svg', el)) b.classList.add('rotate-180');
    bind();
    showEmpty();
    projectChat = Number(ctx.ui('chat_coding')) || null;
    const projectPrefix = `projects/chat-${projectChat}/`;
    const explicit = store.openFile || '';
    store.openFile = null;
    const last = ctx.ui('editor_file') || '';
    // default: the open coding chat's project; an explicitly requested file decides the scope itself
    const startMode = explicit ? (projectChat && explicit.startsWith(projectPrefix) ? 'project' : 'unified')
      : (projectChat ? 'project' : 'unified');
    await setMode(startMode, { quiet: true });
    const want = explicit || (mode === 'project' ? (last.startsWith(projectPrefix) ? last : '') : last);
    if (want) await openFile(want).catch(() => showEmpty());
    unsubs.push(on('repo:changed', () => reloadTree()));
    window.addEventListener('beforeunload', beforeUnload);
  },
  unmount() {
    while (unsubs.length) unsubs.pop()();
    window.removeEventListener('beforeunload', beforeUnload);
    cm = null; cur = null; root = null; docs.clear(); children.clear();
  },
};

function beforeUnload(e) { if (isDirty()) { e.preventDefault(); e.returnValue = ''; } }
function isDirty() { return !!(cm && cur && cur.kind === 'text' && !cm.getDoc().isClean(cur.cleanGen)); }

// ------------------------------------------------------------------ scope (unified vs chat project)
async function setMode(next, { quiet = false } = {}) {
  if (next === 'project') {
    if (!projectChat) { if (!quiet) toast('Open a coding chat first - each coding chat gets its own project folder.', 'warn'); next = 'unified'; }
    else {
      try {
        const r = await api('POST', '/api/coding/projects/ensure', { chat_no: projectChat });
        projectRoot = r.path;
      } catch (e) { if (!quiet) toast(e.message, 'warn'); projectChat = null; projectRoot = null; next = 'unified'; }
    }
  }
  mode = next;
  for (const b of $$('[data-mode]', root)) b.classList.toggle('active', b.dataset.mode === mode);
  const pb = $('#ed-mode-project', root);
  pb.disabled = !projectChat;
  $('#ed-project-label', root).textContent = projectChat ? `chat#${projectChat} project` : 'Chat project';
  $('#ed-scope', root).textContent = mode === 'project' ? `projects/chat-${projectChat}` : 'Workspace';
  selectedDir = mode === 'project' ? projectRoot : (selectedDir && !selectedDir.startsWith('projects/chat-') ? selectedDir : 'scratch');
  if (mode === 'project') expanded.add(projectRoot);
  $('#ed-target', root).textContent = selectedDir;
  children.clear();
  await reloadTree();
}

async function reloadTree() {
  if (!root) return;
  const rootRel = scopeRoot();
  const dirs = new Set([rootRel, ...[...expanded].filter((d) => !rootRel || d === rootRel || d.startsWith(rootRel + '/'))]);
  await Promise.all([...dirs].map((d) => loadDir(d).catch(() => children.delete(d))));
  renderTree();
}

// ------------------------------------------------------------------ tree
async function loadDir(path) {
  const r = await api('GET', `/api/coding/files/list?path=${encodeURIComponent(path)}`);
  children.set(path, r.entries);
}

function rowActions(path, type) {
  const acts = [];
  if (!isContainer(path)) acts.push(['rename', 'pen', 'Rename']);
  if (!isRoot(path)) acts.push(['copy', 'copy', 'Duplicate']);
  acts.push(['download', 'upload', type === 'dir' ? 'Download as .zip' : 'Download']);
  if (!isRoot(path)) acts.push(['delete', 'trash', 'Delete']);
  return acts.map(([a, ic, t]) => `<button class="p-1 rounded-md hover:bg-slate-900/[0.07] dark:hover:bg-white/10 ${a === 'delete' ? 'text-rose-500' : 'text-slate-500'}" data-row="${a}" title="${t}" aria-label="${t}">${icon(ic, `w-3.5 h-3.5 ${a === 'download' ? 'rotate-180' : ''}`)}</button>`).join('');
}

function renderTree() {
  const host = root && $('#ed-tree', root);
  if (!host) return;
  host.innerHTML = '';
  const rootRel = scopeRoot();
  // root node
  const top = document.createElement('div');
  top.className = 'flex items-center gap-1.5 rounded-lg px-1.5 py-1.5 font-semibold text-[12px] text-slate-700 dark:text-slate-200';
  top.dataset.drop = rootRel || 'scratch';
  top.innerHTML = `<span class="text-accent-500">${icon(mode === 'project' ? 'code' : 'layers', 'w-4 h-4')}</span>
    <span class="truncate flex-1">${esc(mode === 'project' ? `chat#${projectChat} project` : 'workspace')}</span>
    ${rootRel ? `<button class="p-1 rounded-md hover:bg-slate-900/[0.07] dark:hover:bg-white/10 text-slate-500" data-row="download" title="Download project as .zip" aria-label="Download project as .zip">${icon('upload', 'w-3.5 h-3.5 rotate-180')}</button>` : ''}`;
  top.querySelectorAll('[data-row]').forEach((b) => b.addEventListener('click', (ev) => { ev.stopPropagation(); rowAction(b.dataset.row, rootRel, 'dir'); }));
  top.addEventListener('click', () => { selectedDir = rootRel || 'scratch'; $('#ed-target', root).textContent = selectedDir; renderTree(); });
  host.appendChild(top);
  const walk = (dir, depth) => {
    const entries = children.get(dir) || [];
    if (!entries.length && dir === rootRel) {
      const empty = document.createElement('div');
      empty.className = 'text-[11px] text-slate-500 py-2';
      empty.style.paddingLeft = `${10 + depth * 14}px`;
      empty.textContent = mode === 'project' ? 'Empty project - create a file, drop files here or apply code from the chat.' : 'Empty';
      host.appendChild(empty);
    }
    for (const e of entries) {
      const row = document.createElement('div');
      const isDir = e.type === 'dir';
      const open = isDir && expanded.has(e.path);
      row.className = `group flex items-center gap-1.5 rounded-lg pr-1 py-1 cursor-pointer hover:bg-white/60 dark:hover:bg-white/[0.06] ${cur && cur.path === e.path ? 'bg-accent-500/15' : ''} ${isDir && selectedDir === e.path ? 'ring-1 ring-accent-500/40' : ''}`;
      row.style.paddingLeft = `${6 + depth * 14}px`;
      row.dataset.drop = isDir ? e.path : dir;
      row.setAttribute('role', 'treeitem');
      const ic = isDir ? 'folder' : (e.kind === 'image' ? 'image' : 'file');
      row.innerHTML = `<span class="w-3 text-slate-400">${isDir ? icon(open ? 'chevron-down' : 'chevron-right', 'w-3 h-3') : ''}</span>
        <span class="${isDir ? 'text-amber-500' : 'text-slate-400'}">${icon(ic, 'w-4 h-4')}</span>
        <span class="truncate flex-1">${esc(e.name)}</span>
        <span class="hidden group-hover:flex items-center">${rowActions(e.path, e.type)}</span>
        ${!isDir && e.size != null ? `<span class="text-[10px] text-slate-400 group-hover:hidden">${fmtBytes(e.size)}</span>` : ''}`;
      row.querySelectorAll('[data-row]').forEach((b) => b.addEventListener('click', (ev) => { ev.stopPropagation(); rowAction(b.dataset.row, e.path, e.type); }));
      row.addEventListener('click', async () => {
        if (isDir) {
          selectedDir = e.path;
          $('#ed-target', root).textContent = selectedDir;
          if (expanded.has(e.path)) expanded.delete(e.path);
          else { expanded.add(e.path); if (!children.has(e.path)) await loadDir(e.path).catch((x) => toast(x.message, 'error')); }
          renderTree();
        } else openFile(e.path);
      });
      host.appendChild(row);
      if (open) walk(e.path, depth + 1);
    }
  };
  walk(rootRel, 1);
}

function rowAction(act, path, type) {
  if (act === 'rename') renameDialog(path, type);
  else if (act === 'copy') copyDialog(path, type);
  else if (act === 'download') download(path);
  else if (act === 'delete') removePath(path, type === 'dir');
}

// ------------------------------------------------------------------ drag & drop upload
function bindDrop() {
  const tree = $('#ed-tree', root);
  let lastHi = null;
  const hasFiles = (e) => e.dataTransfer && [...(e.dataTransfer.types || [])].includes('Files');
  const hi = (elx) => { if (lastHi && lastHi !== elx) lastHi.classList.remove('ring-2', 'ring-accent-500', 'bg-accent-500/10'); if (elx) elx.classList.add('ring-2', 'ring-accent-500', 'bg-accent-500/10'); lastHi = elx; };
  tree.addEventListener('dragover', (e) => { if (!hasFiles(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; hi(e.target.closest('[data-drop]') || tree); });
  tree.addEventListener('dragleave', (e) => { if (!tree.contains(e.relatedTarget)) hi(null); });
  tree.addEventListener('drop', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    const target = e.target.closest('[data-drop]');
    hi(null);
    const dir = target ? target.dataset.drop : (selectedDir || scopeRoot() || 'scratch');
    uploadFiles([...e.dataTransfer.files], dir || 'scratch');
  });
}

async function uploadFiles(files, dir) {
  if (!files.length) return;
  const real = files.filter((f) => f.size > 0 || f.type);
  if (real.length < files.length) toast('Folders cannot be dropped - drop the files inside them instead.', 'warn', 6000);
  if (!real.length) return;
  const fd = new FormData();
  fd.append('dir', dir);
  for (const f of real.slice(0, 20)) fd.append('files', f);
  if (real.length > 20) toast('Only the first 20 files are uploaded at once.', 'warn');
  toast(`Uploading ${Math.min(real.length, 20)} file(s) to ${dir} - scanning with ClamAV…`, 'info');
  try {
    const r = await api('POST', '/api/coding/files/upload', fd);
    const rows = r.results.map((x) => `<tr><td class="font-mono text-xs">${esc(x.name)}</td>
      <td>${x.ok ? `<span class="badge-green">${icon('check', 'w-3 h-3')} uploaded</span>` : '<span class="badge-red">rejected</span>'}</td>
      <td class="text-xs">${x.ok ? esc(x.path || '') + (x.verdict ? ` · ${esc(x.verdict)}` : '') : esc(x.error || '')}</td></tr>`).join('');
    const bad = r.results.filter((x) => !x.ok).length;
    modal({ title: `Upload to ${dir}: ${r.results.length - bad} ok, ${bad} rejected`, wide: true,
      body: `<div class="overflow-x-auto"><table class="table-glass"><thead><tr><th>File</th><th>Result</th><th>Details</th></tr></thead><tbody>${rows}</tbody></table></div>
        <p class="mt-2 text-xs text-slate-500">Every upload is scanned by ClamAV; text files also pass the dependency gate.</p>` });
    expanded.add(dir);
    await loadDir(dir);
    renderTree();
  } catch (ex) { toast(ex.message, 'error'); }
}

async function upload(e) {
  const files = [...e.target.files];
  e.target.value = '';
  await uploadFiles(files, selectedDir || scopeRoot() || 'scratch');
}

// ------------------------------------------------------------------ open / view
function showEmpty() {
  const main = root && $('#ed-main', root);
  if (!main) return;
  main.innerHTML = `<div class="h-full flex flex-col items-center justify-center text-center p-6 text-slate-500">
    <div class="text-slate-400">${icon('code', 'w-10 h-10')}</div><p class="mt-3 text-sm">Open a file from the tree, create one, or drop files onto a folder.</p>
    <p class="mt-1 text-xs">Every save is checked by OSV.dev + ClamAV before it touches disk.</p></div>`;
  $('#ed-path', root).textContent = 'No file open';
  $('#ed-meta', root).textContent = '';
}

export async function openFile(path) {
  if (!root) { store.openFile = path; return; }
  if (isDirty() && !(await confirmModal('Discard unsaved changes?', `${cur.path} has unsaved edits.`, { danger: true, confirmText: 'Discard' }))) return;
  if (mode === 'project' && projectRoot && !path.startsWith(projectRoot + '/')) await setMode('unified', { quiet: true });
  let f;
  try { f = await api('GET', `/api/coding/files/read?path=${encodeURIComponent(path)}`); } catch (e) { toast(e.message, 'error'); return; }
  const parts = f.path.split('/');
  for (let i = 1; i < parts.length; i++) {
    const d = parts.slice(0, i).join('/');
    if (!expanded.has(d)) { expanded.add(d); if (!children.has(d)) await loadDir(d).catch(() => {}); }
  }
  selectedDir = parentOf(f.path) || 'scratch';
  $('#ed-target', root).textContent = selectedDir;
  C.saveUi('editor_file', f.path);
  hideGate();
  if (f.kind === 'text') showText(f);
  else if (f.kind === 'image') showImage(f);
  else showBinary(f);
  renderTree();
  if (!$('#ed-history', root).classList.contains('hidden')) loadHistory();
}

function ensureCm() {
  const main = $('#ed-main', root);
  if (cm && main.contains(cm.getWrapperElement())) return cm;
  main.innerHTML = '<div class="absolute inset-0" id="ed-cm"></div>';
  cm = window.CodeMirror($('#ed-cm', root), {
    lineNumbers: true, lineWrapping: false, styleActiveLine: true, matchBrackets: true, autoCloseBrackets: true,
    autoCloseTags: true, foldGutter: true, gutters: ['CodeMirror-linenumbers', 'CodeMirror-foldgutter'], indentUnit: 4,
    tabSize: 4, indentWithTabs: false, placeholder: 'Start typing…', viewportMargin: 30,
    extraKeys: {
      'Ctrl-S': () => save(), 'Cmd-S': () => save(),
      'Ctrl-/': 'toggleComment', 'Cmd-/': 'toggleComment',
      'Ctrl-Y': 'redo', 'Shift-Ctrl-Z': 'redo', 'Shift-Cmd-Z': 'redo',
      Tab: (c) => (c.somethingSelected() ? c.indentSelection('add') : c.replaceSelection(' '.repeat(c.getOption('indentUnit')), 'end')),
      'Shift-Tab': (c) => c.indentSelection('subtract'),
    },
  });
  cm.addOverlay(fnOverlay);
  cm.on('changes', () => updateMeta());
  return cm;
}

function showText(f) {
  const editorInst = ensureCm();
  let doc = docs.get(f.path);
  const mode_ = langOf(f.path);
  const sep = f.content.includes('\r\n') ? '\r\n' : null;
  if (!doc || doc.__sha !== f.sha) {
    doc = window.CodeMirror.Doc(f.content, mode_, 0, sep);
    doc.__sha = f.sha;
    doc.__cleanGen = doc.changeGeneration(true);
    docs.set(f.path, doc);
  }
  editorInst.swapDoc(doc);
  const unit = mode_ === 'python' ? 4 : 2;
  editorInst.setOption('indentUnit', unit);
  editorInst.setOption('tabSize', unit);
  cur = { path: f.path, sha: f.sha, kind: 'text', cleanGen: doc.__cleanGen, size: f.size, modified: f.modified, mode: mode_ };
  setTimeout(() => { editorInst.refresh(); editorInst.focus(); }, 0);
  updateMeta();
}

function showImage(f) {
  cm = null;
  cur = { path: f.path, sha: f.sha, kind: 'image', size: f.size };
  const main = $('#ed-main', root);
  const src = `/api/coding/files/raw?path=${encodeURIComponent(f.path)}&v=${encodeURIComponent(f.sha || f.modified || '')}`;
  main.innerHTML = `<div class="h-full flex flex-col"><div class="flex-1 min-h-0 flex items-center justify-center p-6 overflow-auto">
      <div class="rounded-2xl p-3 bg-[length:16px_16px] bg-[linear-gradient(45deg,rgba(148,163,184,.25)_25%,transparent_25%,transparent_75%,rgba(148,163,184,.25)_75%),linear-gradient(45deg,rgba(148,163,184,.25)_25%,transparent_25%,transparent_75%,rgba(148,163,184,.25)_75%)] bg-[position:0_0,8px_8px]">
      <img src="${esc(src)}" alt="${esc(f.name)}" class="max-w-full max-h-[60vh] object-contain"></div></div>
    <div class="p-3 text-xs text-slate-500 flex items-center gap-2 border-t border-slate-900/[0.06] dark:border-white/[0.07]">
      <span>${icon('image', 'w-4 h-4')}</span><span>${esc(f.name)} · ${fmtBytes(f.size)}</span>${f.content != null ? `<button class="btn-ghost btn-sm ml-auto" data-svg-src>${icon('code')} Edit SVG source</button>` : ''}</div></div>`;
  $('[data-svg-src]', main)?.addEventListener('click', () => showText({ ...f, kind: 'text' }));
  $('#ed-path', root).textContent = f.path;
  $('#ed-meta', root).textContent = `image · ${fmtBytes(f.size)} · sandboxed preview`;
}

function showBinary(f) {
  cm = null;
  cur = { path: f.path, kind: f.kind, size: f.size };
  $('#ed-main', root).innerHTML = `<div class="h-full flex flex-col items-center justify-center text-center p-6">
    <div class="text-slate-400">${icon('file', 'w-10 h-10')}</div><p class="mt-3 font-medium">${esc(f.name)}</p>
    <p class="text-sm text-slate-500">${f.kind === 'large' ? 'Too large for the editor (2 MB max)' : 'Binary file'} · ${fmtBytes(f.size)}</p>
    <button class="btn-ghost btn-sm mt-4" data-dl>${icon('upload', 'w-4 h-4 rotate-180')} Download</button></div>`;
  $('[data-dl]', root).addEventListener('click', () => download(f.path));
  $('#ed-path', root).textContent = f.path;
  $('#ed-meta', root).textContent = f.kind;
}

function updateMeta() {
  if (!root || !cur || cur.kind !== 'text' || !cm) return;
  const dirty = isDirty();
  const h = cm.historySize();
  $('#ed-path', root).textContent = `${dirty ? '● ' : ''}${cur.path}`;
  $('#ed-meta', root).textContent = `${cur.mode || 'plain text'} · ${cm.lineCount()} lines · ${dirty ? 'unsaved changes' : 'saved'}${cur.modified ? ' · ' + fmtRel(new Date(cur.modified * 1000).toISOString()) : ''}`;
  const u = $('[data-act="undo"]', root); const r = $('[data-act="redo"]', root);
  if (u) u.disabled = !h.undo;
  if (r) r.disabled = !h.redo;
}

// ------------------------------------------------------------------ save / gate banner
function showBanner(html, tone) {
  const g = $('#ed-gate', root);
  g.className = `shrink-0 px-4 py-2 text-sm border-b border-slate-900/[0.06] dark:border-white/[0.07] ${tone === 'ok' ? 'bg-emerald-500/10' : tone === 'bad' ? 'bg-rose-500/10' : 'bg-accent-500/10'}`;
  g.innerHTML = `<div class="flex items-center gap-2 flex-wrap">${html}<span class="flex-1"></span><button class="btn-plain btn-icon btn-sm" data-gate-close aria-label="Dismiss">${icon('x')}</button></div>`;
  $('[data-gate-close]', g).addEventListener('click', hideGate);
  return g;
}

function showGate(report, ok) {
  const warn = (report.warnings || []).length ? ` · ${report.warnings.length} warning(s)` : '';
  const g = showBanner(`${verdictBadge(report.verdict)}<span class="min-w-0 truncate">${esc(report.message || '')}${esc(warn)}</span><button class="btn-ghost btn-sm" data-gate-details>Details</button>`, ok ? 'ok' : 'bad');
  $('[data-gate-details]', g).addEventListener('click', () => gateModal(report));
}
function hideGate() { const g = root && $('#ed-gate', root); if (g) g.classList.add('hidden'); }

async function save(force = false) {
  if (!cur || cur.kind !== 'text' || !cm) return;
  const b = $('[data-act="save"]', root);
  b.disabled = true;
  const doc = cm.getDoc();
  const content = doc.getValue(doc.lineSeparator());
  try {
    const r = await api('POST', '/api/coding/files/save', { path: cur.path, content, base_sha: cur.sha, force });
    cur.sha = r.sha; doc.__sha = r.sha;
    cur.cleanGen = doc.changeGeneration(true); doc.__cleanGen = cur.cleanGen;
    cur.modified = Math.floor(Date.now() / 1000);
    showGate(r.report, true);
    toast(`Saved · ${r.report.message}`, 'ok');
    updateMeta();
    loadDir(parentOf(cur.path)).then(renderTree).catch(() => {});
    if (!$('#ed-history', root).classList.contains('hidden')) loadHistory();
  } catch (e) {
    if (e.status === 422 && e.data.report) { showGate(e.data.report, false); gateModal(e.data.report); }
    else if (e.status === 409) {
      const choice = await modal({ title: 'File changed on disk', body: '<p>The file was changed elsewhere since you opened it (or already exists).</p>',
        actions: [{ label: 'Cancel', value: null }, { label: 'Reload from disk', value: 'reload' }, { label: 'Overwrite (still gated)', kind: 'danger', value: 'force' }] });
      if (choice === 'reload') { docs.delete(cur.path); cm.getDoc().markClean(); cur.cleanGen = cm.getDoc().changeGeneration(true); await openFile(cur.path); }
      else if (choice === 'force') await save(true);
    } else toast(e.message, 'error');
  } finally { b.disabled = false; }
}

// ------------------------------------------------------------------ dialogs
async function promptValue(title, label, value, selectStem = false) {
  const box = document.createElement('div');
  box.innerHTML = `<label class="label">${label}</label><input class="input font-mono" id="ed-name" autocomplete="off" value="${esc(value)}">
    <p class="hint">Names can't contain &lt; &gt; : " / \\ | ? * or reserved device names; .git is protected.</p>`;
  const input = box.querySelector('#ed-name');
  setTimeout(() => { input.focus(); const dot = value.lastIndexOf('.'); input.setSelectionRange(0, selectStem && dot > 0 ? dot : value.length); }, 30);
  const ok = await modal({ title, body: box, actions: [{ label: 'Cancel', value: false }, { label: 'OK', kind: 'primary', value: true }] });
  return ok ? (input.value || '').trim() : '';
}

async function createEntry(kind) {
  const dir = selectedDir || scopeRoot() || 'scratch';
  const name = await promptValue(kind === 'file' ? 'New file' : 'New folder', `Name (inside <span class="font-mono">${esc(dir)}</span>)`, kind === 'file' ? 'app.py' : 'src', true);
  if (!name) return;
  const path = `${dir}/${name.replace(/^\/+/, '')}`;
  try {
    await api('POST', '/api/coding/files/new', { path, kind });
    expanded.add(dir);
    await loadDir(parentOf(path));
    if (kind === 'folder') { expanded.add(path); selectedDir = path; $('#ed-target', root).textContent = path; await loadDir(path); }
    renderTree();
    if (kind === 'file') openFile(path);
  } catch (e) { if (e.status === 422 && e.data.report) gateModal(e.data.report); else toast(e.message, 'error'); }
}

async function moveWithConfirm(kind, path, newPath) {
  const url = kind === 'rename' ? '/api/coding/files/rename' : '/api/coding/files/copy';
  try {
    return await api('POST', url, { path, new_path: newPath, overwrite: false });
  } catch (e) {
    if (e.status === 409 && e.data.code === 'exists') {
      if (e.data.is_dir) { toast('A folder with that name already exists - folders are never overwritten.', 'warn', 6000); return null; }
      const ok = await confirmModal('Overwrite existing file?', `${e.data.target} already exists. Overwrite it? (Its current content is kept in the version history.)`, { danger: true, confirmText: 'Overwrite' });
      if (!ok) return null;
      try { return await api('POST', url, { path, new_path: newPath, overwrite: true }); } catch (e2) { moveError(e2); return null; }
    }
    moveError(e);
    return null;
  }
}
function moveError(e) { if (e.status === 422 && e.data.report) gateModal(e.data.report, 'Security gate'); else toast(e.message, 'error', 7000); }

function remapPath(oldP, newP) {
  const map = (p) => (p === oldP ? newP : p.startsWith(oldP + '/') ? newP + p.slice(oldP.length) : p);
  for (const [k, d] of [...docs]) { const nk = map(k); if (nk !== k) { docs.delete(k); docs.set(nk, d); } }
  for (const d of [...expanded]) { const nd = map(d); if (nd !== d) { expanded.delete(d); expanded.add(nd); } }
  if (cur) cur.path = map(cur.path);
  selectedDir = map(selectedDir);
}

async function renameDialog(path, type) {
  if (isContainer(path)) { toast('Workspace containers (scratch, repos, projects, clones and chat projects) cannot be renamed.', 'warn'); return; }
  const name = await promptValue(`Rename ${type === 'dir' ? 'folder' : 'file'}`, `New name for <span class="font-mono">${esc(path)}</span>`, nameOf(path), type !== 'dir');
  if (!name || name === nameOf(path)) return;
  const newPath = `${parentOf(path)}/${name}`;
  const r = await moveWithConfirm('rename', path, newPath);
  if (!r) return;
  remapPath(path, r.path);
  await loadDir(parentOf(path)).catch(() => {});
  if (parentOf(r.path) !== parentOf(path)) await loadDir(parentOf(r.path)).catch(() => {});
  renderTree();
  if (cur) { $('#ed-path', root).textContent = cur.path; C.saveUi('editor_file', cur.path); }
  const g = showBanner(`${icon('pen', 'w-4 h-4')}<span class="min-w-0 truncate">Renamed <span class="font-mono">${esc(r.from)}</span> → <span class="font-mono">${esc(r.path)}</span>${r.gate ? ' · gate GO' : ''}</span><button class="btn-ghost btn-sm" data-undo>${icon('undo')} Undo</button>`, 'info');
  $('[data-undo]', g).addEventListener('click', async () => {
    const back = await moveWithConfirm('rename', r.path, r.from);
    if (!back) return;
    remapPath(r.path, r.from);
    await loadDir(parentOf(r.from)).catch(() => {});
    renderTree();
    if (cur) $('#ed-path', root).textContent = cur.path;
    hideGate();
    toast('Rename undone', 'ok');
  });
}

async function copyDialog(path, type) {
  const base = nameOf(path);
  const dot = type === 'dir' ? -1 : base.lastIndexOf('.');
  const suggestion = dot > 0 ? `${base.slice(0, dot)} copy${base.slice(dot)}` : `${base} copy`;
  const target = await promptValue(`Duplicate ${type === 'dir' ? 'folder' : 'file'}`, 'Copy to (workspace path)', `${parentOf(path) || 'scratch'}/${suggestion}`);
  if (!target) return;
  const r = await moveWithConfirm('copy', path, target);
  if (!r) return;
  expanded.add(parentOf(r.path));
  await loadDir(parentOf(r.path)).catch(() => {});
  renderTree();
  toast(`Copied to ${r.path}${r.type === 'dir' ? ` (${r.files} files, ${r.manifests_gated} manifest(s) gated)` : ''}`, 'ok', 6000);
}

function download(path) {
  const a = document.createElement('a');
  a.href = `/api/coding/files/download?path=${encodeURIComponent(path)}`;
  a.rel = 'noopener';
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function removePath(path, isDir) {
  if (!(await confirmModal(`Delete ${isDir ? 'folder' : 'file'}?`, `${path} will be deleted.${isDir ? ' Everything inside it is removed.' : ' A copy is kept in its version history.'}`, { danger: true, confirmText: 'Delete' }))) return;
  try {
    await api('POST', '/api/coding/files/delete', { path });
    docs.delete(path);
    if (cur && (cur.path === path || cur.path.startsWith(path + '/'))) { cur = null; cm = null; showEmpty(); }
    expanded.delete(path);
    await loadDir(parentOf(path)).catch(() => {});
    renderTree();
    toast('Deleted', 'ok');
  } catch (e) { toast(e.message, 'error'); }
}

// ------------------------------------------------------------------ toolbar wiring
function bind() {
  root.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-act]');
    if (b) {
      const act = b.dataset.act;
      if (act === 'save') save();
      else if (act === 'undo' && cm) { cm.undo(); cm.focus(); }
      else if (act === 'redo' && cm) { cm.redo(); cm.focus(); }
      else if (act === 'history') toggleHistory();
      else if (act === 'delete' && cur) removePath(cur.path, false);
      else if (act === 'rename-cur' && cur) renameDialog(cur.path, 'file');
      else if (act === 'download-cur' && cur) download(cur.path);
      else if (act === 'new-file') createEntry('file');
      else if (act === 'new-folder') createEntry('folder');
      else if (act === 'refresh') reloadTree();
      else if (act === 'toggle-tree') { const s = $('#ed-side', root); s.classList.toggle('hidden'); s.classList.toggle('flex'); }
      return;
    }
    const m = e.target.closest('[data-mode]');
    if (m && !m.disabled && m.dataset.mode !== mode) {
      if (isDirty() && !(await confirmModal('Discard unsaved changes?', `${cur.path} has unsaved edits.`, { danger: true, confirmText: 'Discard' }))) return;
      await setMode(m.dataset.mode);
    }
  });
  $('#ed-upload', root).addEventListener('change', upload);
  bindDrop();
}

// ------------------------------------------------------------------ history
function toggleHistory() {
  const h = $('#ed-history', root);
  h.classList.toggle('hidden'); h.classList.toggle('flex');
  if (!h.classList.contains('hidden')) loadHistory();
}

async function loadHistory() {
  const list = $('#ed-hist-list', root);
  if (!cur) { list.innerHTML = '<p class="text-slate-500 p-2">Open a file to see its history.</p>'; return; }
  const r = await api('GET', `/api/coding/files/history?path=${encodeURIComponent(cur.path)}`);
  if (!r.versions.length) { list.innerHTML = '<p class="text-slate-500 p-2">No earlier versions yet - each save, rename and copy snapshots the previous content.</p>'; return; }
  list.innerHTML = r.versions.map((v) => `<button class="w-full text-left card !p-2 hover:ring-1 hover:ring-accent-500/40" data-ver="${v.id}">
    <div class="flex items-center gap-1.5"><b>#${v.id}</b><span class="chip !h-4 !text-[10px]">${esc(v.reason)}</span><span class="ml-auto text-[10px] text-slate-500">${esc(fmtRel(v.created_at))}</span></div>
    <div class="text-[10px] text-slate-500 mt-0.5">${v.existed ? fmtBytes(v.size) + ' before this change' : 'did not exist at this point'} · ${esc(fmtDate(v.created_at))}</div></button>`).join('');
  for (const b of $$('[data-ver]', list)) b.addEventListener('click', () => previewVersion(Number(b.dataset.ver)));
}

async function previewVersion(id) {
  const v = await api('GET', `/api/coding/files/version/${id}`);
  const body = document.createElement('div');
  if (v.existed && v.content != null) {
    const pre = document.createElement('pre');
    pre.className = 'max-h-[50vh] overflow-auto scroll-thin rounded-2xl bg-[#0d1117] text-slate-100 p-3 text-[12px] cm-s-default';
    const md = langOf(v.path);
    if (window.CodeMirror && CodeMirror.runMode && md) CodeMirror.runMode(v.content, md, pre); else pre.textContent = v.content;
    body.appendChild(pre);
  } else body.innerHTML = '<p>At this point the file did not exist. Restoring removes it (a snapshot is kept).</p>';
  const ok = await modal({ title: `${v.path} · version #${v.id} (${fmtDate(v.created_at)})`, body, wide: true,
    actions: [{ label: 'Close', value: false }, { label: 'Restore this version (gated)', kind: 'primary', value: true }] });
  if (!ok) return;
  try {
    const r = await api('POST', '/api/coding/files/restore', { version_id: id });
    docs.delete(v.path);
    toast(r.deleted ? 'File removed (restored to before it existed)' : 'Version restored', 'ok');
    if (r.deleted) { cur = null; cm = null; showEmpty(); await loadDir(parentOf(v.path)); renderTree(); }
    else { if (cm && cur) { cm.getDoc().markClean(); cur.cleanGen = cm.getDoc().changeGeneration(true); } await openFile(v.path); }
  } catch (e) { if (e.status === 422 && e.data.report) gateModal(e.data.report); else toast(e.message, 'error'); }
}
