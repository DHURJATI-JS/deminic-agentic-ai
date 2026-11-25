// Top navigation bar - a thin gradient bar that sweeps left -> right across the
// top of the page whenever you navigate: page links, chat / section / tab
// switches inside the app, and every page arrival. Like NProgress / YouTube.
//
//   start()  begin (nested calls are counted)     done()  finish one
//   track(promise)  start, then finish when the promise settles
//   pulse()  a quick full sweep (instant view switches)

let root = null;
let fill = null;
let val = 0;
let active = 0;
let trickle = 0;
let hideT = 0;
const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function ensure() {
  if (root && root.isConnected) return root;
  root = document.createElement('div');
  root.id = 'nav-progress';
  root.setAttribute('aria-hidden', 'true');
  fill = document.createElement('div');
  fill.className = 'nav-progress-fill';
  root.appendChild(fill);
  document.body.appendChild(root);
  return root;
}

function set(v) {
  val = Math.min(Math.max(v, val), 1);
  fill.style.width = `${(val * 100).toFixed(2)}%`;
}

export function start() {
  ensure();
  active += 1;
  clearTimeout(hideT);
  if (!root.classList.contains('on') || root.classList.contains('finishing')) {
    root.classList.remove('finishing');
    val = 0;
    fill.style.transition = 'none';
    fill.style.width = '0%';
    void fill.offsetWidth; // restart from the left edge
    fill.style.transition = '';
    root.classList.add('on');
    set(reduced ? 0.5 : 0.12);
  }
  clearInterval(trickle);
  // ease towards the end but never reach it until done() is called
  trickle = setInterval(() => set(val + (0.994 - val) * (val < 0.4 ? 0.14 : val < 0.75 ? 0.06 : 0.02)), 200);
}

export function done(force = false) {
  if (!root) return;
  active = force ? 0 : Math.max(0, active - 1);
  if (active) return;
  clearInterval(trickle);
  trickle = 0;
  set(1);
  root.classList.add('finishing');
  hideT = setTimeout(() => { root.classList.remove('on', 'finishing'); val = 0; }, 520);
}

export async function track(promise) {
  start();
  try { return await promise; } finally { done(); }
}

export function pulse() { start(); setTimeout(() => done(), 260); }

function sameDocumentNav(a, e) {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return false;
  if (a.target && a.target !== '_self') return false;
  if (a.hasAttribute('download') || a.dataset.noProgress !== undefined) return false;
  const href = a.getAttribute('href') || '';
  if (!href || href.startsWith('#') || href.startsWith('javascript:') || href.startsWith('mailto:')) return false;
  let url;
  try { url = new URL(a.href, location.href); } catch { return false; }
  if (url.origin !== location.origin) return false;
  // same page, only the hash changes: no page load
  if (url.pathname === location.pathname && url.search === location.search && url.hash) return false;
  return true;
}

export function initProgress() {
  // page links: start the sweep as soon as the click happens
  document.addEventListener('click', (e) => {
    const a = e.target.closest?.('a[href]');
    if (a && sameDocumentNav(a, e)) start();
  });
  // page arrival: finish the sweep that the previous page started
  const arrive = () => { start(); setTimeout(() => done(true), reduced ? 0 : 340); };
  if (document.readyState === 'complete') arrive(); else window.addEventListener('load', arrive, { once: true });
  // back/forward cache restores a page with the bar frozen mid-way
  window.addEventListener('pageshow', (e) => { if (e.persisted) done(true); });
}
