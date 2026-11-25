// Chat card renderers for the personal tools.
import { api, toast } from '../../core.js';
import { cardShell } from '../../cards.js';
import { bindMeet, meetBlock } from './calendar.js';
import { editEmail, send as sendEmail } from './family.js';
import { movieTile, stationTile, watchBlock } from './media.js';
import { el, esc, refLink, STATUS_BADGE, timeOnly } from './util.js';
import { relFuture } from './tasks.js';

const ACTION_LABEL = { created: 'Task created', queued: 'Task queued (same name is active)', completed: 'Task done', activated: 'Now active (left the queue)', deleted: 'Task deleted' };

function taskBody(t) {
  return `<div class="flex flex-wrap items-center gap-2"><span class="font-medium ${t.status === 'done' ? 'line-through text-slate-400' : ''}">${esc(t.title)}</span>
      ${t.status ? `<span class="${STATUS_BADGE[t.status] || 'badge-gray'}">${esc(t.status)}</span>` : ''}${t.stale ? '<span class="badge-amber">stale</span>' : ''}</div>
    <div class="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-slate-500">${refLink(t.ref)}
      ${t.due_local ? `<span>⏰ ${esc(t.due_local)} · ${esc(relFuture(t.due_at))}</span>` : ''}
      ${t.location_name ? `<span>📍 ${esc(t.location_name)}</span>` : ''}
      ${t.queued_behind_no ? `<span class="text-violet-600 dark:text-violet-300">⏳ waiting behind ${refLink('task#' + t.queued_behind_no)}</span>` : ''}
      ${t.reminder && t.reminder.can_cancel ? '<span>🔔 reminder set</span>' : ''}</div>`;
}

function taskCard(c) {
  const t = c.task || {};
  const node = el(cardShell(`<div class="flex items-start gap-3"><div class="text-lg">${c.action === 'queued' ? '⏳' : c.action === 'completed' ? '✅' : c.action === 'deleted' ? '🗑️' : '📝'}</div>
    <div class="min-w-0 flex-1"><div class="text-[11px] uppercase tracking-wide text-slate-500">${esc(ACTION_LABEL[c.action] || 'Task')}</div>${taskBody(t)}</div>
    ${t.status === 'active' ? '<button class="btn-ghost btn-sm" data-done>Done</button>' : ''}</div>`));
  node.querySelector('[data-done]')?.addEventListener('click', async (e) => {
    e.currentTarget.disabled = true;
    try {
      const r = await api('POST', `/api/personal/tasks/${t.task_no}/complete`);
      toast(r.activated ? `${t.ref} done · ${r.activated.ref} now active` : `${t.ref} done`, 'ok');
      e.target.closest('.rounded-2xl').querySelector('.font-medium')?.classList.add('line-through', 'text-slate-400');
    } catch (ex) { toast(ex.message, 'error'); }
  });
  return node;
}

function taskList(c) {
  const rows = (c.tasks || []).map((t) => `<li class="py-1.5 border-t first:border-t-0 border-slate-900/[0.05] dark:border-white/[0.06]">${taskBody(t)}</li>`).join('');
  return cardShell(`<div class="text-[11px] uppercase tracking-wide text-slate-500">${esc(c.status || 'open')} tasks · ${(c.tasks || []).length}</div>
    ${rows ? `<ul class="mt-1">${rows}</ul>` : '<p class="text-xs text-slate-500 mt-1">No tasks.</p>'}`);
}

function eventCard(c) {
  const e = c.event || {};
  const node = el(cardShell(`<div class="flex items-start gap-3"><div class="text-lg">📅</div><div class="min-w-0 flex-1">
      <div class="text-[11px] uppercase tracking-wide text-slate-500">${c.action === 'meet_attached' ? 'Google Meet attached' : 'Calendar event'}</div>
      <div class="font-medium">${esc(e.title)}</div>
      <div class="text-[11px] text-slate-500">${esc(e.start_local)} → ${esc(timeOnly(e.ends_at))}${e.location ? ' · 📍 ' + esc(e.location) : ''} · ${refLink(e.ref)}</div>
      <div class="meet-host">${meetBlock(e)}</div>
      <div class="mt-2 flex flex-wrap gap-1.5"><a class="btn-primary btn-sm no-underline" href="${esc(e.google_url)}" target="_blank" rel="noopener noreferrer">Add to Google Calendar</a>
        <a class="btn-ghost btn-sm no-underline" href="${esc(e.ics_url)}" download>Download .ics</a></div></div></div>`));
  bindMeet(node, e, async () => {
    try {
      const r = await api('GET', `/api/personal/calendar?start=${e.starts_at.slice(0, 10)}&end=${new Date(new Date(e.starts_at).getTime() + 2 * 86400000).toISOString().slice(0, 10)}`);
      const fresh = r.events.find((x) => x.event_no === e.event_no);
      if (fresh) node.replaceWith(eventCard({ ...c, action: 'meet_attached', event: fresh }));
    } catch { /* ignore */ }
  });
  return node;
}

function eventList(c) {
  const ev = (c.events || []).map((e) => `<li class="py-1">📅 <b>${esc(e.title)}</b> <span class="text-[11px] text-slate-500">${esc(e.start_local)}</span> ${refLink(e.ref)}${e.needs_meet_link ? ' <span class="badge-amber">needs Meet link</span>' : ''}</li>`).join('');
  const tk = (c.tasks || []).map((t) => `<li class="py-1">☐ ${esc(t.title)} <span class="text-[11px] text-slate-500">${esc(t.due_local)}</span> ${refLink(t.ref)}</li>`).join('');
  return cardShell(`<div class="text-[11px] uppercase tracking-wide text-slate-500">Schedule</div>${ev || tk ? `<ul class="mt-1 text-sm">${ev}${tk}</ul>` : '<p class="text-xs text-slate-500">Nothing planned.</p>'}`);
}

function contactCard(c) {
  const p = c.contact || {};
  return cardShell(`<div class="flex items-center gap-3"><div class="w-10 h-10 rounded-2xl bg-gradient-to-br from-sky-400 to-accent-500 text-white font-semibold flex items-center justify-center">${esc((p.name || '?').slice(0, 1).toUpperCase())}</div>
    <div class="min-w-0"><div class="text-[11px] uppercase tracking-wide text-slate-500">Family profile saved to global memory</div><div class="font-medium">${esc(p.name)} <span class="text-xs text-slate-500 capitalize">${esc(p.relation || '')}</span></div>
    <div class="text-[11px] text-slate-500 break-all">${[p.email, p.phone, p.birthday ? '🎂 ' + p.birthday : ''].filter(Boolean).map(esc).join(' · ')}</div></div></div>`);
}

function emailCard(c) {
  const m = c.email || {};
  const node = el(cardShell(`<div class="flex items-start gap-3"><div class="text-lg">✉️</div><div class="min-w-0 flex-1">
      <div class="flex flex-wrap items-center gap-2"><span class="text-[11px] uppercase tracking-wide text-slate-500">E-mail draft</span><span class="${STATUS_BADGE[m.status] || 'badge-gray'} status">${esc(m.status)}</span>${refLink(m.ref)}</div>
      <div class="text-xs text-slate-500 mt-0.5">to ${esc(m.to_name)} &lt;${esc(m.to_email)}&gt;</div>
      <div class="font-medium mt-1">${esc(m.subject)}</div>
      <div class="mt-1 text-[13px] whitespace-pre-wrap break-words text-slate-700 dark:text-slate-200">${esc(m.body)}</div>
      ${c.smtp === false ? '<div class="mt-2 text-xs text-amber-600">SMTP isn\'t set up yet - <a href="/settings#smtp">configure e-mail</a> to send.</div>' : ''}
      <div class="mt-2 flex flex-wrap gap-1.5">${m.status !== 'sent' ? '<button class="btn-primary btn-sm" data-send>Send</button>' : ''}<button class="btn-ghost btn-sm" data-edit>Edit</button></div></div></div>`));
  node.querySelector('[data-send]')?.addEventListener('click', async (e) => {
    const b = e.currentTarget;
    b.disabled = true;
    const r = await sendEmail(m.email_no);
    if (r && r.ok) { node.querySelector('.status').textContent = 'sent'; node.querySelector('.status').className = 'badge-green status'; b.remove(); }
    else b.disabled = false;
  });
  node.querySelector('[data-edit]').addEventListener('click', () => editEmail(m, () => {}));
  return node;
}

function guideCard(c, ctx) {
  const node = el(cardShell(`<div class="flex items-center gap-2"><span class="text-lg">🧭</span><span class="font-semibold">${esc(c.place)}</span><span class="text-[11px] text-slate-500">local guide</span></div>
    ${c.intro ? `<p class="mt-1 text-[13px] text-slate-600 dark:text-slate-300">${esc(c.intro)}</p>` : ''}
    ${(c.dishes || []).length ? `<div class="mt-2 text-[11px] uppercase tracking-wide text-slate-500">Must-try dishes</div><ul class="mt-1 space-y-1">${c.dishes.map((d) => `<li class="text-[13px]">🍛 <b>${esc(d.name)}</b> - ${esc(d.description || '')}${d.where ? ` <span class="text-slate-500">(${esc(d.where)})</span>` : ''}</li>`).join('')}</ul>` : ''}
    ${(c.places || []).length ? `<div class="mt-2 text-[11px] uppercase tracking-wide text-slate-500">Places</div><ul class="mt-1 space-y-1">${c.places.map((p) => `<li class="text-[13px]">📍 <b>${esc(p.name)}</b> - ${esc(p.why || '')}</li>`).join('')}</ul>` : ''}
    ${(c.tips || []).length ? `<div class="mt-2 text-[11px] uppercase tracking-wide text-slate-500">Tips</div><ul class="mt-1 list-disc pl-5 text-[13px]">${c.tips.map((t) => `<li>${esc(t)}</li>`).join('')}</ul>` : ''}
    ${(c.suggested_tasks || []).length ? `<div class="mt-3 flex flex-wrap gap-1.5">${c.suggested_tasks.map((t, i) => `<button class="btn-ghost btn-sm" data-task="${i}">+ ${esc(t)}</button>`).join('')}</div>` : ''}`));
  node.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-task]');
    if (!b) return;
    b.disabled = true;
    try {
      const r = await api('POST', '/api/personal/tasks', { title: c.suggested_tasks[Number(b.dataset.task)], location_name: c.place, description: `From the ${c.place} guide` });
      b.textContent = `✓ ${r.task.ref}`;
      toast(`${r.task.ref} added`, 'ok');
    } catch (ex) { toast(ex.message, 'error'); b.disabled = false; }
  });
  return node;
}

function movieListCard(c, ctx) {
  const wrap = el(cardShell(`<div class="text-[11px] uppercase tracking-wide text-slate-500">🎬 Recommendations${c.request ? ' · ' + esc(c.request) : ''}</div><div class="mt-2 grid gap-2 movies"></div>`));
  const box = wrap.querySelector('.movies');
  for (const m of c.movies || []) box.appendChild(movieTile(ctx, m));
  return wrap;
}

function movieCard(c, ctx) {
  const v = c.video;
  const w = c.watch || {};
  const node = el(cardShell(`<div class="flex gap-3">${w.poster ? `<img src="${esc(w.poster)}" alt="" class="w-20 h-28 rounded-xl object-cover shrink-0" loading="lazy">` : ''}
    <div class="min-w-0 flex-1"><div class="font-semibold">${esc(c.title)} ${c.year ? `<span class="text-slate-500 font-normal">(${esc(c.year)})</span>` : ''}</div>
      ${w.overview ? `<p class="text-xs text-slate-600 dark:text-slate-300 line-clamp-3 mt-0.5">${esc(w.overview)}</p>` : ''}
      ${v ? `<div class="mt-2 flex items-center gap-2"><img src="${esc(v.thumb)}" alt="" class="w-24 aspect-video rounded-lg object-cover" loading="lazy">
        <div class="min-w-0"><div class="text-xs line-clamp-2">${esc(v.title)}</div><div class="text-[10px] text-slate-500">${esc(v.channel || '')} · YouTube${c.video_kind === 'trailer' ? ' · <span class="text-amber-600">trailer - full movie not on YouTube</span>' : ''}</div>
        <button class="mt-1 btn-primary btn-sm" data-play>▶ ${c.autoplay ? 'Playing - play again' : 'Play on YouTube'}</button></div></div>`
    : `<div class="mt-2 text-xs text-slate-500">Not found on YouTube · <a href="${esc(c.youtube_search)}" target="_blank" rel="noopener noreferrer">search YouTube</a></div>`}
      <div class="mt-2 text-[11px] uppercase tracking-wide text-slate-500">Where to watch${w.region ? ' · ' + esc(w.region) : ''}</div>${watchBlock(w)}</div></div>`));
  node.querySelector('[data-play]')?.addEventListener('click', () => ctx.player.playYouTube({ videoId: v.videoId, title: v.title || c.title }));
  return node;
}

function musicCard(c, ctx) {
  if (c.station) {
    const node = el(cardShell(`<div class="text-[11px] uppercase tracking-wide text-slate-500">🎵 Now playing · ${esc(c.query)}</div><div class="mt-2 space-y-2 st"></div>`));
    const box = node.querySelector('.st');
    box.appendChild(stationTile(ctx, c.station, c.query));
    for (const s of c.more || []) box.appendChild(stationTile(ctx, s, c.query));
    return node;
  }
  if (c.video) {
    const vids = [c.video, ...(c.more_videos || [])];
    const node = el(cardShell(`<div class="text-[11px] uppercase tracking-wide text-slate-500">🎵 ${esc(c.query)} · YouTube</div><div class="mt-2 space-y-1.5 vids"></div>`));
    const box = node.querySelector('.vids');
    vids.forEach((v, i) => {
      const row = el(`<div class="flex items-center gap-2"><img src="${esc(v.thumb)}" alt="" class="w-20 aspect-video rounded-lg object-cover" loading="lazy">
        <div class="min-w-0 flex-1 text-xs line-clamp-2">${esc(v.title)}</div><button class="btn-${i === 0 ? 'primary' : 'ghost'} btn-sm">▶</button></div>`);
      row.querySelector('button').addEventListener('click', () => ctx.player.playYouTube({ videoId: v.videoId, title: v.title }));
      box.appendChild(row);
    });
    return node;
  }
  return cardShell(`🎵 Couldn't find "${esc(c.query)}". ${c.youtube_search ? `<a href="${esc(c.youtube_search)}" target="_blank" rel="noopener noreferrer">Search YouTube</a>` : ''}`);
}

export function registerPersonalCards(registerCard) {
  registerCard('task', taskCard);
  registerCard('task_list', taskList);
  registerCard('event', eventCard);
  registerCard('event_list', eventList);
  registerCard('contact', contactCard);
  registerCard('email_draft', emailCard);
  registerCard('guide', guideCard);
  registerCard('movie_list', movieListCard);
  registerCard('movie', movieCard);
  registerCard('music', musicCard);
}
