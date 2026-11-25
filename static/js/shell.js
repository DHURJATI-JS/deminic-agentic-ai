// Loaded on EVERY page by base.html: the immersive (VR) background, the top
// navigation bar, the system-status button and the immersive toggle.
// Page modules import the same files by the same URL, so each page shares
// one instance of every module (no duplicate canvases or listeners).
import { api, applyTheme, toast } from './core.js';
import { isImmersive, setImmersive } from './immersive.js';
import { initProgress } from './progress.js';
import { bindStatusButtons } from './status.js';

function syncImmersiveToggles() {
  const on = isImmersive();
  for (const b of document.querySelectorAll('[data-immersive-toggle]')) {
    if (b.dataset.immersiveToggle) continue; // fixed on/off buttons keep their look
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    if (b.classList.contains('btn-icon')) b.classList.toggle('text-accent-500', on);
    const label = b.querySelector('[data-immersive-label]');
    if (label) label.textContent = on ? 'Exit immersive mode' : 'Enter immersive mode';
  }
}

/** Turn immersive mode on/off (toggle when `force` is undefined) and remember it -
 *  per account when signed in, per device otherwise (Redis, never browser storage). */
export function toggleImmersive(force, announce = true) {
  const on = force === undefined ? !isImmersive() : !!force;
  const changed = on !== isImmersive();
  setImmersive(on);
  syncImmersiveToggles();
  if (!changed) return;
  api('PUT', '/api/device-prefs', { immersive: on }, { idempotent: false, allow401: true }).catch(() => {});
  if (announce) toast(on ? 'Immersive mode on - starry night in dark mode, birds & clouds in light mode' : 'Immersive mode off', 'info', 3000);
}

/** Home page showcase: jump straight into the night (stars) or day (birds) scene. */
function tryScene(scene) {
  const theme = scene === 'night' ? 'dark' : 'light';
  applyTheme(theme);
  api('PUT', '/api/theme', { theme }, { idempotent: false, allow401: true }).catch(() => {});
  toggleImmersive(true, false);
  toast(scene === 'night' ? 'Night sky - stars, a moon and the odd shooting star' : 'Daytime sky - drifting clouds and birds', 'info', 3000);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function bind() {
  for (const b of document.querySelectorAll('[data-immersive-toggle]')) {
    if (b.dataset.immBound) continue;
    b.dataset.immBound = '1';
    const mode = b.dataset.immersiveToggle;
    b.addEventListener('click', () => toggleImmersive(mode === 'on' ? true : mode === 'off' ? false : undefined));
  }
  for (const b of document.querySelectorAll('[data-try-scene]')) {
    if (b.dataset.sceneBound) continue;
    b.dataset.sceneBound = '1';
    b.addEventListener('click', () => tryScene(b.dataset.tryScene));
  }
  syncImmersiveToggles();
}

initProgress();
bindStatusButtons();
bind();
