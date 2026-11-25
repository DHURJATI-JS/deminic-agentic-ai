import { bindThemeToggles } from './core.js';
import { hydrate, icon } from './icons.js';

bindThemeToggles();
hydrate(document);

// ------------------------------------------------------------------ home: "See it in action" player
// Plays the recorded clips one after another (only while on screen), tabs jump to a clip,
// clicking the video pauses / resumes. Reduced-motion users get a poster + play button.
const video = document.getElementById('demo-video');
if (video) {
  const tabs = [...document.querySelectorAll('[data-demo]')];
  const caption = document.getElementById('demo-caption');
  const toggle = document.getElementById('demo-toggle');
  const bar = document.getElementById('demo-progress');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  let visible = false;
  let userPaused = reduced;

  const paint = () => {
    const playing = !video.paused && !video.ended;
    toggle.innerHTML = icon(playing ? 'pause' : 'play', 'w-6 h-6');
    toggle.setAttribute('aria-label', playing ? 'Pause' : 'Play');
    toggle.classList.toggle('is-playing', playing);
  };
  const play = () => { if (visible && !userPaused) video.play().catch(() => {}); };

  const select = (b, autoplay = true) => {
    for (const t of tabs) {
      const on = t === b;
      t.classList.toggle('active', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    }
    video.poster = b.dataset.poster;
    video.src = b.dataset.src;
    caption.textContent = b.dataset.caption || '';
    bar.style.width = '0%';
    b.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    if (autoplay) play();
  };

  tabs.forEach((b) => b.addEventListener('click', () => { userPaused = false; select(b); }));
  video.addEventListener('ended', () => {
    const i = tabs.findIndex((t) => t.classList.contains('active'));
    select(tabs[(i + 1) % tabs.length]);
  });
  video.addEventListener('timeupdate', () => {
    if (video.duration) bar.style.width = `${(video.currentTime / video.duration) * 100}%`;
  });
  for (const ev of ['play', 'pause', 'ended']) video.addEventListener(ev, paint);
  const flip = () => {
    if (video.paused) { userPaused = false; video.play().catch(() => {}); } else { userPaused = true; video.pause(); }
  };
  video.addEventListener('click', flip);
  toggle.addEventListener('click', flip);
  new IntersectionObserver(([e]) => {
    visible = e.isIntersecting;
    if (visible) play(); else video.pause();
  }, { threshold: 0.35 }).observe(video);
  paint();
}
