// Media tab - movies (play ONLY from YouTube + where to stream) and music
// (radio streams / YouTube) that keep playing alongside the chat.
import { $, $$, api, busy, toast } from '../../core.js';
import { esc } from './util.js';

let root = null;
let C = null;
const GENRES = ['synthwave', 'lofi', 'jazz', 'classical', 'ambient', 'rock', 'pop', 'bollywood', 'edm', 'chillout', 'hip hop', 'retro'];

export async function mount(el, ctx) {
  root = el; C = ctx;
  el.innerHTML = `<div class="h-full overflow-y-auto scroll-thin p-5 md:p-8 space-y-8">
    <section>
      <div class="flex flex-wrap items-end gap-3"><div><h2 class="text-xl font-semibold">Movies</h2>
        <p class="text-sm text-slate-500">Play from YouTube, or see where it streams (Netflix, Prime Video, Hotstar…). Add a TMDB key in Settings for exact availability.</p></div></div>
      <div class="mt-4 grid md:grid-cols-2 gap-3">
        <form id="mv-search" class="card flex gap-2"><input class="input" name="q" placeholder="Search a movie, e.g. Interstellar" maxlength="120"><button class="btn-primary">Search</button></form>
        <form id="mv-rec" class="card flex gap-2"><input class="input" name="q" placeholder="Recommend: feel-good, 90s thrillers, like Inception…" maxlength="300"><button class="btn-ghost">Recommend</button></form>
      </div>
      <div id="mv-results" class="mt-4 grid sm:grid-cols-2 xl:grid-cols-3 gap-3"></div>
    </section>
    <section>
      <h2 class="text-xl font-semibold">Music</h2>
      <p class="text-sm text-slate-500">Pick a vibe - it plays in the mini player while you keep chatting.</p>
      <div class="mt-3 flex flex-wrap gap-1.5">${GENRES.map((g) => `<button class="btn-ghost btn-sm" data-genre="${esc(g)}">${esc(g)}</button>`).join('')}</div>
      <form id="mu-search" class="mt-3 card flex gap-2 max-w-xl"><input class="input" name="q" placeholder="Genre, station or a specific song" maxlength="120"><button class="btn-primary">Find</button></form>
      <div id="mu-results" class="mt-4 grid sm:grid-cols-2 xl:grid-cols-3 gap-3"></div>
    </section></div>`;
  $('#mv-search', el).addEventListener('submit', searchMovies);
  $('#mv-rec', el).addEventListener('submit', recommend);
  $('#mu-search', el).addEventListener('submit', (e) => { e.preventDefault(); findMusic(new FormData(e.target).get('q')); });
  for (const b of $$('[data-genre]', el)) b.addEventListener('click', () => findMusic(b.dataset.genre));
}

export function unmount() { root = null; }

// ------------------------------------------------------------------ shared renderers (also used by chat cards)
export function watchBlock(w) {
  if (!w) return '';
  const groups = [['Stream', w.flatrate], ['Free', w.free], ['Rent', w.rent], ['Buy', w.buy]].filter(([, l]) => l && l.length);
  if (w.source === 'tmdb' && groups.length) {
    return `<div class="mt-2 space-y-1.5 text-xs">${groups.map(([k, l]) => `<div class="flex flex-wrap items-center gap-1.5"><span class="text-slate-500 w-12">${k}</span>
      ${l.map((p) => `<span class="chip !h-6">${p.logo ? `<img src="${esc(p.logo)}" alt="" class="w-4 h-4 rounded" loading="lazy">` : ''}${esc(p.name)}</span>`).join('')}</div>`).join('')}
      ${w.link ? `<a class="text-[11px]" href="${esc(w.link)}" target="_blank" rel="noopener noreferrer">All options in ${esc(w.region)} (JustWatch) ↗</a>` : ''}</div>`;
  }
  return `<div class="mt-2 text-xs"><div class="text-slate-500 mb-1">${w.source === 'tmdb' ? `Not streaming in ${esc(w.region)} right now - search:` : 'Search where it streams:'}</div>
    <div class="flex flex-wrap gap-1.5">${(w.links || []).map((l) => `<a class="btn-ghost btn-sm !h-6 no-underline" href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.name)}</a>`).join('')}</div></div>`;
}

export async function playMovie(ctx, title, year, btn) {
  busy(btn, true);
  try {
    const r = await api('GET', `/api/personal/media/movie-video?title=${encodeURIComponent(title)}&year=${encodeURIComponent(year || '')}`);
    const v = r.video;
    if (v) {
      ctx.player.playYouTube({ videoId: v.videoId, title: v.title || title });
      toast(r.kind === 'trailer' ? `Full movie isn't on YouTube - playing the trailer. Check "Where to watch".` : `Playing from YouTube: ${v.title || title}`, r.kind === 'trailer' ? 'warn' : 'ok', 6000);
    } else { toast('Not found on YouTube - opening a YouTube search', 'warn'); window.open(r.search_url, '_blank', 'noopener'); }
  } catch (e) { toast(e.message, 'error'); } finally { busy(btn, false); }
}

export async function showWatch(title, year, host, btn) {
  busy(btn, true);
  try {
    const w = await api('GET', `/api/personal/media/watch?title=${encodeURIComponent(title)}&year=${encodeURIComponent(year || '')}`);
    host.innerHTML = watchBlock(w);
  } catch (e) { toast(e.message, 'error'); } finally { busy(btn, false); }
}

export function movieTile(ctx, m) {
  const d = document.createElement('div');
  d.className = 'card flex gap-3';
  d.innerHTML = `${m.poster ? `<img src="${esc(m.poster)}" alt="" class="w-20 h-28 rounded-xl object-cover shrink-0" loading="lazy">` : '<div class="w-20 h-28 rounded-xl bg-gradient-to-br from-slate-300 to-slate-400 dark:from-slate-700 dark:to-slate-800 flex items-center justify-center text-2xl shrink-0">🎬</div>'}
    <div class="min-w-0 flex-1"><div class="font-medium">${esc(m.title)} ${m.year ? `<span class="text-slate-500 font-normal">(${esc(m.year)})</span>` : ''}</div>
      ${m.genre ? `<div class="text-[11px] text-slate-500">${esc(m.genre)}</div>` : ''}
      <p class="mt-1 text-xs text-slate-600 dark:text-slate-300 line-clamp-3">${esc(m.why || m.overview || '')}</p>
      <div class="mt-2 flex flex-wrap gap-1.5"><button class="btn-primary btn-sm" data-act="play">▶ Play (YouTube)</button><button class="btn-ghost btn-sm" data-act="watch">Where to watch</button></div>
      <div class="watch"></div></div>`;
  d.querySelector('[data-act="play"]').addEventListener('click', (e) => playMovie(ctx, m.title, m.year, e.currentTarget));
  d.querySelector('[data-act="watch"]').addEventListener('click', (e) => showWatch(m.title, m.year, d.querySelector('.watch'), e.currentTarget));
  return d;
}

function videoTile(ctx, v, label = 'Play') {
  const d = document.createElement('div');
  d.className = 'card !p-2.5 flex gap-3 items-center';
  d.innerHTML = `<img src="${esc(v.thumb)}" alt="" class="w-28 aspect-video rounded-lg object-cover shrink-0" loading="lazy">
    <div class="min-w-0 flex-1"><div class="text-sm font-medium line-clamp-2">${esc(v.title)}</div><div class="text-[11px] text-slate-500">${esc(v.channel || '')} ${esc(v.duration || '')}</div>
    <button class="mt-1.5 btn-primary btn-sm">▶ ${esc(label)}</button></div>`;
  d.querySelector('button').addEventListener('click', () => ctx.player.playYouTube({ videoId: v.videoId, title: v.title }));
  return d;
}

export function stationTile(ctx, s, query) {
  const d = document.createElement('div');
  d.className = 'card !p-3 flex gap-3 items-center';
  d.innerHTML = `<div class="w-12 h-12 rounded-2xl bg-gradient-to-br from-fuchsia-500 to-accent-500 flex items-center justify-center text-white text-xl overflow-hidden shrink-0">${s.favicon ? `<img src="${esc(s.favicon)}" alt="" class="w-full h-full object-cover" data-fallback>` : '♫'}</div>
    <div class="min-w-0 flex-1"><div class="text-sm font-medium truncate">${esc(s.name)}</div>
      <div class="text-[11px] text-slate-500 truncate">${esc(s.country || '')} · ${esc(s.codec || '')} ${s.bitrate ? esc(s.bitrate) + 'kbps' : ''} ${s.https ? '' : '· http'}</div>
      <div class="text-[10px] text-slate-400 truncate">${esc(s.tags || '')}</div></div>
    <button class="btn-primary btn-icon btn-sm !rounded-full" aria-label="Play ${esc(s.name)}">▶</button>`;
  d.querySelector('img[data-fallback]')?.addEventListener('error', (e) => e.target.remove());
  d.querySelector('button').addEventListener('click', () => ctx.player.playAudio({ title: s.name, url: s.url, subtitle: `${query || 'radio'} · ${s.country || 'online'}`, homepage: s.homepage, favicon: s.favicon }));
  return d;
}

// ------------------------------------------------------------------ actions
async function searchMovies(e) {
  e.preventDefault();
  const q = new FormData(e.target).get('q').trim();
  if (!q) return;
  const box = $('#mv-results', root);
  box.innerHTML = '<p class="text-sm text-slate-500">Searching…</p>';
  try {
    const r = await api('GET', `/api/personal/media/movies?q=${encodeURIComponent(q)}`);
    box.innerHTML = '';
    if (r.source === 'tmdb') r.movies.forEach((m) => box.appendChild(movieTile(C, m)));
    else (r.videos || []).forEach((v) => box.appendChild(videoTile(C, v)));
    if (!box.children.length) box.innerHTML = '<p class="text-sm text-slate-500">Nothing found.</p>';
  } catch (ex) { box.innerHTML = `<p class="text-sm text-rose-600">${esc(ex.message)}</p>`; }
}

async function recommend(e) {
  e.preventDefault();
  const q = new FormData(e.target).get('q').trim() || 'a great movie for tonight';
  const box = $('#mv-results', root);
  box.innerHTML = '<p class="text-sm text-slate-500">Thinking of good movies…</p>';
  try {
    const r = await api('POST', '/api/personal/media/recommend', { request: q, count: 6 });
    box.innerHTML = '';
    r.movies.forEach((m) => box.appendChild(movieTile(C, m)));
  } catch (ex) { box.innerHTML = `<p class="text-sm text-rose-600">${esc(ex.message)}</p>`; }
}

async function findMusic(q) {
  q = String(q || '').trim();
  if (!q) return;
  const box = $('#mu-results', root);
  box.innerHTML = '<p class="text-sm text-slate-500">Tuning in…</p>';
  try {
    const r = await api('GET', `/api/personal/media/radio?q=${encodeURIComponent(q)}`);
    box.innerHTML = '';
    r.stations.forEach((s) => box.appendChild(stationTile(C, s, q)));
    if (!r.stations.length) {
      const y = await api('GET', `/api/personal/media/youtube?q=${encodeURIComponent(q)}`);
      y.videos.forEach((v) => box.appendChild(videoTile(C, v)));
      if (!y.videos.length) box.innerHTML = '<p class="text-sm text-slate-500">Nothing found.</p>';
    }
  } catch (ex) { box.innerHTML = `<p class="text-sm text-rose-600">${esc(ex.message)}</p>`; }
}
