// Command palette (Ctrl/Cmd + K): fuzzy jump to sections, tabs, chats and actions.
import { esc } from './core.js';
import { icon } from './icons.js';

function score(q, text) {
  if (!q) return 1;
  const t = text.toLowerCase();
  const s = q.toLowerCase();
  if (t.includes(s)) return 100 - t.indexOf(s);
  let i = 0;
  let gaps = 0;
  for (const ch of t) {
    if (ch === s[i]) i++;
    else if (i) gaps++;
    if (i === s.length) return 50 - gaps;
  }
  return 0;
}

let open = null;

export function openPalette(getItems) {
  if (open) { open.close(); return; }
  const root = document.getElementById('cmdk-root') || document.body;
  const back = document.createElement('div');
  back.className = 'cmdk-back';
  back.innerHTML = `<div class="cmdk" role="dialog" aria-modal="true" aria-label="Command palette">
    <div class="flex items-center gap-2 pl-5 text-slate-400">${icon('search', 'w-5 h-5')}
      <input class="cmdk-input !pl-2" placeholder="Jump to a section, tab, chat or action..." aria-label="Search commands" autocomplete="off" spellcheck="false">
      <span class="kbd mr-4">Esc</span></div>
    <div class="cmdk-list" role="listbox"></div>
    <div class="flex items-center gap-3 px-5 h-10 text-[11px] text-slate-400" style="border-top:1px solid var(--ring)">
      <span class="kbd">&uarr;</span><span class="kbd">&darr;</span> navigate <span class="kbd">Enter</span> open <span class="ml-auto">Deminic</span></div></div>`;
  root.appendChild(back);
  const input = back.querySelector('input');
  const list = back.querySelector('.cmdk-list');
  const items = getItems();
  let shown = [];
  let active = 0;

  const paint = () => {
    const q = input.value.trim();
    shown = items.map((it) => ({ it, s: score(q, `${it.label} ${it.group} ${it.keywords || ''}`) })).filter((x) => x.s > 0)
      .sort((a, b) => (q ? b.s - a.s : 0)).map((x) => x.it).slice(0, 60);
    active = Math.min(active, Math.max(0, shown.length - 1));
    let html = '';
    let group = '';
    shown.forEach((it, i) => {
      if (it.group !== group) { group = it.group; html += `<div class="cmdk-group">${esc(group)}</div>`; }
      html += `<div class="cmdk-item ${i === active ? 'active' : ''}" data-i="${i}" role="option" aria-selected="${i === active}">
        <span class="icon-badge-soft !w-8 !h-8 !rounded-lg">${icon(it.icon || 'sparkles', 'w-4 h-4')}</span>
        <span class="flex-1 min-w-0 truncate">${esc(it.label)}</span>${it.hint ? `<span class="text-[11px] text-slate-400 truncate max-w-[40%]">${esc(it.hint)}</span>` : ''}</div>`;
    });
    list.innerHTML = html || '<div class="px-4 py-10 text-center text-sm text-slate-400">No matches</div>';
    list.querySelector('.cmdk-item.active')?.scrollIntoView({ block: 'nearest' });
  };
  const close = () => { back.remove(); document.removeEventListener('keydown', onKey, true); open = null; };
  const run = (i) => { const it = shown[i]; if (!it) return; close(); setTimeout(() => it.run(), 0); };
  const onKey = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); close(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); active = (active + 1) % Math.max(shown.length, 1); paint(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); active = (active - 1 + shown.length) % Math.max(shown.length, 1); paint(); }
    else if (e.key === 'Enter') { e.preventDefault(); run(active); }
  };
  document.addEventListener('keydown', onKey, true);
  input.addEventListener('input', () => { active = 0; paint(); });
  list.addEventListener('click', (e) => { const el = e.target.closest('[data-i]'); if (el) run(Number(el.dataset.i)); });
  list.addEventListener('mousemove', (e) => { const el = e.target.closest('[data-i]'); if (el && Number(el.dataset.i) !== active) { active = Number(el.dataset.i); paint(); } });
  back.addEventListener('click', (e) => { if (e.target === back) close(); });
  paint();
  input.focus();
  open = { close };
}
