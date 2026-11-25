// Persistent mini player: music keeps playing alongside the chat while you
// switch tabs/chats; movies play from YouTube (privacy-enhanced embed).
import { $, esc } from './core.js';

function box() { return $('#player'); }

export const player = {
  current: null,
  playAudio({ title = 'Now playing', subtitle = '', url, homepage = '', favicon = '' }) {
    if (!url) return;
    const b = box();
    this.current = { kind: 'audio', title, url };
    b.innerHTML = `<div class="flex items-center gap-3">
      <div class="w-11 h-11 rounded-2xl bg-gradient-to-br from-fuchsia-500 to-accent-500 flex items-center justify-center text-white text-lg overflow-hidden">
        ${favicon ? `<img src="${esc(favicon)}" alt="" class="w-full h-full object-cover" data-hide-on-error>` : '♫'}</div>
      <div class="min-w-0 flex-1"><div class="text-sm font-semibold truncate">${esc(title)}</div>
        <div class="text-xs text-slate-500 truncate">${esc(subtitle)} ${homepage ? `· <a href="${esc(homepage)}" target="_blank" rel="noopener noreferrer">site</a>` : ''}</div></div>
      <button class="btn-plain btn-icon btn-sm" data-close aria-label="Stop">&times;</button></div>
      <audio class="mt-2 w-full h-9" controls autoplay preload="none" src="${esc(url)}"></audio>`;
    b.querySelector('img[data-hide-on-error]')?.addEventListener('error', (e) => e.target.remove());
    b.querySelector('[data-close]').addEventListener('click', () => this.close());
    b.classList.remove('hidden');
    b.querySelector('audio').play().catch(() => { /* autoplay may need a click */ });
  },
  playYouTube({ videoId, title = 'YouTube', start = 0 }) {
    if (!/^[A-Za-z0-9_-]{6,20}$/.test(videoId || '')) return;
    const b = box();
    this.current = { kind: 'youtube', title, videoId };
    b.innerHTML = `<div class="flex items-center gap-2 mb-2"><span class="text-red-500">▶</span>
      <div class="text-sm font-semibold truncate flex-1">${esc(title)}</div>
      <button class="btn-plain btn-icon btn-sm" data-expand title="Expand" aria-label="Expand">⤢</button>
      <button class="btn-plain btn-icon btn-sm" data-close aria-label="Close">&times;</button></div>
      <div class="relative w-full rounded-2xl overflow-hidden bg-black aspect-video">
      <iframe class="absolute inset-0 w-full h-full" src="https://www.youtube-nocookie.com/embed/${encodeURIComponent(videoId)}?autoplay=1&rel=0&start=${Number(start) || 0}"
        title="${esc(title)}" allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe></div>`;
    b.querySelector('[data-close]').addEventListener('click', () => this.close());
    b.querySelector('[data-expand]').addEventListener('click', () => b.classList.toggle('w-[min(820px,calc(100vw-32px))]'));
    b.classList.remove('hidden');
  },
  close() {
    const b = box();
    b.innerHTML = '';
    b.classList.add('hidden');
    this.current = null;
  },
};
window.deminicPlayer = player;
