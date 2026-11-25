// Chat "cards" registry - tools return cards (task created, weather, e-mail draft ...)
// and section modules register a renderer per card type.
import { esc, el, fmtDate } from './core.js';

const renderers = {};

export function registerCard(type, fn) { renderers[type] = fn; }

export function renderCards(container, cards, ctx) {
  container.innerHTML = '';
  for (const card of cards || []) {
    const fn = renderers[card.type] || renderers.__default;
    let out;
    try { out = fn(card, ctx); } catch (e) { console.error('card render failed', card, e); out = renderers.__default(card, ctx); }
    if (!out) continue;
    const node = typeof out === 'string' ? el(out) : out;
    if (node) container.appendChild(node);
  }
}

export const cardShell = (inner, cls = '') => `<div class="mt-3 rounded-2xl glass-soft p-3 text-sm ${cls}">${inner}</div>`;

registerCard('__default', (c) => cardShell(`<div class="text-xs text-slate-500">${esc(c.type)}</div><div class="mt-1">${esc(c.title || c.text || '')}</div>`));

registerCard('memory', (c) => cardShell(
  c.action === 'deleted'
    ? `🧹 Forgot <b>${esc(c.label)}</b>`
    : `🧠 Saved to global memory: <b>[${esc(c.category)}] ${esc(c.key)}</b> ${c.sensitive ? '<span class="badge-amber ml-1">encrypted · sensitive</span>' : ''}`));

registerCard('law', (c) => cardShell(`⚖️ <b>Lawyer mode</b> · ${esc(c.dispute_type || 'dispute')} &mdash; if this needs more than the local model can do it is handed to an online model automatically.`));

registerCard('website', (c) => cardShell(c.ok
  ? `🌐 Read <a href="${esc(c.url)}" target="_blank" rel="noopener noreferrer">${esc(c.title || c.url)}</a> <span class="text-xs text-slate-500">(${Number(c.chars || 0).toLocaleString()} chars, injection-filtered)</span>`
  : `🌐 Couldn't read <span class="font-mono text-xs">${esc(c.url)}</span>: ${esc(c.error || 'error')}`));

registerCard('weather', (c) => {
  const cur = c.current || {};
  const days = (c.days || []).map((d) => `<div class="text-center min-w-[64px]"><div class="text-[11px] text-slate-500">${esc(fmtDate(d.date, false))}</div>
    <div class="text-xl">${esc(d.icon)}</div><div class="text-xs">${Math.round(d.min)}° / <b>${Math.round(d.max)}°</b></div>
    <div class="text-[10px] text-sky-600">${d.rain_pct ?? '-'}% rain</div></div>`).join('');
  return cardShell(`<div class="flex items-center gap-4"><div class="text-4xl">${esc(cur.icon || '')}</div>
    <div><div class="text-xs text-slate-500">${esc(c.place)}</div><div class="text-3xl font-semibold">${Math.round(cur.temp)}°C</div>
    <div class="text-xs text-slate-500">${esc(cur.desc)} · feels ${Math.round(cur.feels)}° · ${cur.humidity}% humidity · wind ${cur.wind} km/h</div></div></div>
    <div class="mt-3 flex gap-2 overflow-x-auto scroll-thin">${days}</div>`);
});
