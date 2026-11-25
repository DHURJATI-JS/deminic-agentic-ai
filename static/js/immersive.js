// Immersive mode - a realistic, animated environment behind the glass UI.
// Night (dark theme): layered twinkling stars, milky-way band, nebula, glowing
// moon, shooting stars.  Day (light theme): time-of-day sky, sun glow,
// volumetric clouds, distant hills, flocks of birds.  Parallax follows the
// pointer / Quest controller.  Pauses when hidden; static frame for reduced motion.
const S = { on: false, cv: null, cx: null, w: 0, h: 0, dpr: 1, raf: 0, last: 0, night: true, scene: null,
  px: 0, py: 0, tx: 0, ty: 0, reduced: window.matchMedia('(prefers-reduced-motion: reduce)').matches };
const rnd = (a, b) => a + Math.random() * (b - a);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

// ------------------------------------------------------------------ night
function buildNight() {
  const { w, h } = S;
  const n = clamp(Math.round((w * h) / 2300), 260, 950);
  const stars = [];
  for (let i = 0; i < n; i++) {
    const z = Math.random() < 0.62 ? 0 : Math.random() < 0.75 ? 1 : 2;
    const tint = Math.random();
    stars.push({ x: Math.random(), y: Math.random(), z, r: z === 0 ? rnd(0.35, 0.8) : z === 1 ? rnd(0.7, 1.2) : rnd(1.1, 1.8),
      a: rnd(0.35, 1), sp: rnd(0.6, 2.4), ph: rnd(0, Math.PI * 2),
      c: tint < 0.12 ? '255,214,170' : tint < 0.3 ? '186,210,255' : '255,255,255' });
  }
  // milky way: dense dim dust along a diagonal band
  const band = [];
  for (let i = 0; i < n * 1.4; i++) {
    const t = Math.random();
    const off = (Math.random() + Math.random() + Math.random() - 1.5) * 0.11;
    band.push({ x: t, y: 0.15 + t * 0.62 + off, a: rnd(0.06, 0.32), r: rnd(0.3, 0.8) });
  }
  // nebula pre-rendered once (cheap to draw every frame)
  const neb = document.createElement('canvas');
  neb.width = Math.max(320, Math.round(w / 2)); neb.height = Math.max(240, Math.round(h / 2));
  const g = neb.getContext('2d');
  const blobs = [[0.25, 0.3, '120,90,255'], [0.68, 0.55, '40,140,255'], [0.45, 0.72, '200,60,200'], [0.82, 0.25, '20,200,190']];
  for (const [bx, by, col] of blobs) {
    const rad = Math.max(neb.width, neb.height) * rnd(0.35, 0.55);
    const gr = g.createRadialGradient(bx * neb.width, by * neb.height, 0, bx * neb.width, by * neb.height, rad);
    gr.addColorStop(0, `rgba(${col},0.22)`); gr.addColorStop(0.45, `rgba(${col},0.08)`); gr.addColorStop(1, `rgba(${col},0)`);
    g.fillStyle = gr; g.fillRect(0, 0, neb.width, neb.height);
  }
  return { stars, band, neb, shooting: [], nextShot: performance.now() + rnd(2500, 6000) };
}

function drawMoon(ctx, x, y, r) {
  const glow = ctx.createRadialGradient(x, y, r * 0.6, x, y, r * 5);
  glow.addColorStop(0, 'rgba(210,225,255,0.28)'); glow.addColorStop(0.35, 'rgba(160,190,255,0.08)'); glow.addColorStop(1, 'rgba(120,150,255,0)');
  ctx.fillStyle = glow; ctx.beginPath(); ctx.arc(x, y, r * 5, 0, Math.PI * 2); ctx.fill();
  const body = ctx.createRadialGradient(x - r * 0.35, y - r * 0.35, r * 0.1, x, y, r);
  body.addColorStop(0, '#fbfcff'); body.addColorStop(0.7, '#dfe5f0'); body.addColorStop(1, '#bcc6d8');
  ctx.fillStyle = body; ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = 'rgba(120,132,160,0.18)';
  for (const [cx, cy, cr] of [[-0.3, -0.1, 0.18], [0.25, 0.3, 0.13], [0.1, -0.38, 0.09], [-0.15, 0.4, 0.1], [0.38, -0.12, 0.07]]) {
    ctx.beginPath(); ctx.arc(x + cx * r, y + cy * r, cr * r, 0, Math.PI * 2); ctx.fill();
  }
  // soft terminator (gibbous phase)
  const sh = ctx.createLinearGradient(x - r, y, x + r, y);
  sh.addColorStop(0, 'rgba(6,8,18,0)'); sh.addColorStop(0.72, 'rgba(6,8,18,0)'); sh.addColorStop(1, 'rgba(6,8,18,0.55)');
  ctx.fillStyle = sh; ctx.beginPath(); ctx.arc(x, y, r + 0.5, 0, Math.PI * 2); ctx.fill();
}

function renderNight(t) {
  const { cx: ctx, w, h, scene } = S;
  const bg = ctx.createLinearGradient(0, 0, 0, h);
  bg.addColorStop(0, '#03040b'); bg.addColorStop(0.55, '#070b1d'); bg.addColorStop(1, '#0d1430');
  ctx.fillStyle = bg; ctx.fillRect(0, 0, w, h);
  ctx.globalAlpha = 0.9;
  ctx.drawImage(scene.neb, S.px * -10, S.py * -8, w + 20, h + 16);
  ctx.globalAlpha = 1;
  for (const d of scene.band) {
    ctx.fillStyle = `rgba(220,226,255,${d.a})`;
    ctx.fillRect(d.x * w + S.px * -6, d.y * h + S.py * -4, d.r, d.r);
  }
  for (const s of scene.stars) {
    const tw = 0.55 + 0.45 * Math.sin(t * 0.001 * s.sp + s.ph);
    const a = s.a * (0.45 + 0.55 * tw);
    const x = ((s.x * w + S.px * -(4 + s.z * 9) + t * 0.0012 * (s.z + 1)) % w + w) % w;
    const y = s.y * h + S.py * -(3 + s.z * 7);
    ctx.fillStyle = `rgba(${s.c},${a})`;
    if (s.r < 1) ctx.fillRect(x, y, s.r * 1.6, s.r * 1.6);
    else {
      ctx.beginPath(); ctx.arc(x, y, s.r, 0, Math.PI * 2); ctx.fill();
      if (s.z === 2 && tw > 0.85) { ctx.fillStyle = `rgba(${s.c},${a * 0.25})`; ctx.fillRect(x - s.r * 3, y - 0.4, s.r * 6, 0.8); ctx.fillRect(x - 0.4, y - s.r * 3, 0.8, s.r * 6); }
    }
  }
  // phones / portrait: keep the moon in the gap between the top bar and the page heading
  const narrow = w < 640;
  drawMoon(ctx, w * (narrow ? 0.8 : 0.84) + S.px * -14, (narrow ? 104 : h * 0.17) + S.py * -10, clamp(Math.min(w, h) * 0.045, 16, 48));
  // shooting stars
  if (!S.reduced && t > scene.nextShot) {
    const ang = rnd(2.55, 2.95);
    scene.shooting.push({ x: rnd(w * 0.3, w * 1.05), y: rnd(-20, h * 0.35), vx: Math.cos(ang) * rnd(0.9, 1.4), vy: -Math.sin(ang) * -rnd(0.35, 0.6), life: 0, max: rnd(700, 1200) });
    scene.nextShot = t + rnd(3500, 9000);
  }
  scene.shooting = scene.shooting.filter((s) => s.life < s.max);
  for (const s of scene.shooting) {
    s.life += 16;
    const p = s.life / s.max;
    const x = s.x + s.vx * s.life * 0.9, y = s.y + s.vy * s.life * 0.9;
    const len = 140 * (1 - p * 0.4);
    const gr = ctx.createLinearGradient(x, y, x - s.vx * len, y - s.vy * len);
    gr.addColorStop(0, `rgba(255,255,255,${0.9 * (1 - p)})`); gr.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.strokeStyle = gr; ctx.lineWidth = 1.6; ctx.lineCap = 'round';
    ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x - s.vx * len, y - s.vy * len); ctx.stroke();
  }
}

// ------------------------------------------------------------------ day
function skyPalette() {
  const hr = new Date().getHours() + new Date().getMinutes() / 60;
  if (hr < 7) return { top: '#6f8fe8', mid: '#a9b8f0', low: '#ffd2bd', sun: [0.18, 0.62], sunC: '255,190,150' };
  if (hr < 10) return { top: '#4f9bff', mid: '#9fcbff', low: '#ffe3cc', sun: [0.22, 0.32], sunC: '255,236,200' };
  if (hr < 16) return { top: '#2f8cff', mid: '#7fbfff', low: '#dff1ff', sun: [0.72, 0.16], sunC: '255,250,235' };
  if (hr < 19) return { top: '#4a74e0', mid: '#f0a2a8', low: '#ffc98a', sun: [0.8, 0.5], sunC: '255,196,120' };
  return { top: '#3a4fa8', mid: '#9a8fd8', low: '#ffb9a0', sun: [0.85, 0.66], sunC: '255,170,140' };
}

function makeCloud(scale) {
  const c = document.createElement('canvas');
  const W = Math.round(260 * scale), H = Math.round(120 * scale);
  // puffs reach ~0.25 H above, ~0.1 H below and ~0.05 W beside the core box:
  // pad the sprite so no cloud is ever cut by a straight edge
  const padX = Math.round(W * 0.12), padTop = Math.round(H * 0.4), padBottom = Math.round(H * 0.15);
  c.width = W + padX * 2; c.height = H + padTop + padBottom;
  const g = c.getContext('2d');
  g.translate(padX, padTop);
  const puffs = 14 + Math.round(Math.random() * 10);
  for (let i = 0; i < puffs; i++) {
    const x = rnd(W * 0.15, W * 0.85), y = rnd(H * 0.35, H * 0.7) - Math.sin((x / W) * Math.PI) * H * 0.2;
    const r = rnd(H * 0.18, H * 0.38);
    const gr = g.createRadialGradient(x, y - r * 0.2, r * 0.1, x, y, r);
    gr.addColorStop(0, 'rgba(255,255,255,0.95)'); gr.addColorStop(0.6, 'rgba(255,255,255,0.55)'); gr.addColorStop(1, 'rgba(255,255,255,0)');
    g.fillStyle = gr; g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2); g.fill();
  }
  const shade = g.createLinearGradient(0, H * 0.45, 0, H);
  shade.addColorStop(0, 'rgba(160,180,215,0)'); shade.addColorStop(1, 'rgba(150,170,210,0.35)');
  g.globalCompositeOperation = 'source-atop'; g.fillStyle = shade; g.fillRect(-padX, -padTop, c.width, c.height);
  return c;
}

function hillPath(seed, base, amp) {
  const pts = [];
  for (let i = 0; i <= 24; i++) {
    const t = i / 24;
    pts.push([t, base - amp * (0.5 + 0.5 * Math.sin(t * 6.2 + seed) * Math.cos(t * 2.7 + seed * 1.7))]);
  }
  return pts;
}

function buildDay() {
  const { w } = S;
  const clouds = [];
  const n = clamp(Math.round(w / 220), 5, 11);
  for (let i = 0; i < n; i++) {
    const depth = Math.random();
    clouds.push({ img: makeCloud(0.6 + depth * 1.1), x: Math.random(), y: rnd(0.05, 0.5), depth, sp: 0.000004 + depth * 0.00001 });
  }
  return { pal: skyPalette(), clouds, hills: [hillPath(1.3, 0.86, 0.07), hillPath(4.1, 0.91, 0.06), hillPath(7.7, 0.96, 0.05)],
    flocks: [], nextFlock: performance.now() + rnd(1500, 4000) };
}

function drawBird(ctx, x, y, s, flap, alpha) {
  const wing = Math.sin(flap) * 0.55;
  ctx.strokeStyle = `rgba(30,41,59,${alpha})`; ctx.lineWidth = Math.max(1, s * 0.14); ctx.lineCap = 'round';
  ctx.beginPath();
  ctx.moveTo(x - s, y - s * wing);
  ctx.quadraticCurveTo(x - s * 0.45, y - s * (0.35 + wing * 0.5), x, y);
  ctx.quadraticCurveTo(x + s * 0.45, y - s * (0.35 + wing * 0.5), x + s, y - s * wing);
  ctx.stroke();
}

function renderDay(t) {
  const { cx: ctx, w, h, scene } = S;
  const p = scene.pal;
  const bg = ctx.createLinearGradient(0, 0, 0, h);
  bg.addColorStop(0, p.top); bg.addColorStop(0.55, p.mid); bg.addColorStop(1, p.low);
  ctx.fillStyle = bg; ctx.fillRect(0, 0, w, h);
  const sx = p.sun[0] * w + S.px * -12, sy = p.sun[1] * h + S.py * -10;
  const R = Math.max(w, h);
  const glow = ctx.createRadialGradient(sx, sy, 0, sx, sy, R * 0.55);
  glow.addColorStop(0, `rgba(${p.sunC},0.95)`); glow.addColorStop(0.05, `rgba(${p.sunC},0.55)`); glow.addColorStop(0.25, `rgba(${p.sunC},0.16)`); glow.addColorStop(1, `rgba(${p.sunC},0)`);
  ctx.fillStyle = glow; ctx.fillRect(0, 0, w, h);
  for (const c of scene.clouds) {
    c.x -= c.sp * 16;
    if (c.x < -0.4) { c.x = 1.05; c.y = rnd(0.05, 0.5); }
    const cw = c.img.width, chh = c.img.height;
    ctx.globalAlpha = 0.55 + c.depth * 0.45;
    ctx.drawImage(c.img, c.x * w + S.px * -(6 + c.depth * 16), c.y * h + S.py * -(4 + c.depth * 10), cw, chh);
  }
  ctx.globalAlpha = 1;
  // birds
  if (!S.reduced && t > scene.nextFlock && scene.flocks.length < 3) {
    const depth = Math.random();
    const dir = Math.random() < 0.5 ? 1 : -1;
    const count = 3 + Math.floor(Math.random() * 5);
    const birds = [];
    for (let i = 0; i < count; i++) birds.push({ ox: -i * rnd(14, 22) * (0.5 + depth), oy: (i % 2 ? 1 : -1) * Math.ceil(i / 2) * rnd(8, 13) * (0.5 + depth), ph: rnd(0, 6) });
    scene.flocks.push({ x: dir > 0 ? -0.1 : 1.1, y: rnd(0.12, 0.45), dir, sp: (0.00004 + depth * 0.00006), size: 3 + depth * 6, depth, birds });
    scene.nextFlock = t + rnd(5000, 12000);
  }
  scene.flocks = scene.flocks.filter((f) => f.x > -0.3 && f.x < 1.3);
  for (const f of scene.flocks) {
    f.x += f.sp * 16 * f.dir;
    const baseX = f.x * w + S.px * -(4 + f.depth * 12), baseY = f.y * h + Math.sin(t * 0.0009 + f.depth * 5) * 6 + S.py * -(3 + f.depth * 8);
    for (const b of f.birds) drawBird(ctx, baseX + b.ox * f.dir, baseY + b.oy, f.size, t * 0.012 + b.ph, 0.35 + f.depth * 0.5);
  }
  // atmospheric hills
  const cols = ['rgba(120,150,190,0.55)', 'rgba(96,132,150,0.65)', 'rgba(78,116,110,0.75)'];
  scene.hills.forEach((pts, i) => {
    ctx.fillStyle = cols[i];
    ctx.beginPath(); ctx.moveTo(-20, h + 10);
    for (const [px, py] of pts) ctx.lineTo(px * (w + 40) - 20 + S.px * -(4 + i * 6), py * h + S.py * -(2 + i * 3));
    ctx.lineTo(w + 20, h + 10); ctx.closePath(); ctx.fill();
  });
}

// ------------------------------------------------------------------ engine
function frame(t) {
  S.raf = 0;
  if (!S.on || document.hidden) return;
  S.px += (S.tx - S.px) * 0.04; S.py += (S.ty - S.py) * 0.04;
  (S.night ? renderNight : renderDay)(t);
  if (!S.reduced) S.raf = requestAnimationFrame(frame);
}

function build() {
  S.night = document.documentElement.classList.contains('dark');
  S.scene = S.night ? buildNight() : buildDay();
}

function resize() {
  if (!S.cv) return;
  S.dpr = Math.min(window.devicePixelRatio || 1, 1.5);
  S.w = window.innerWidth; S.h = window.innerHeight;
  S.cv.width = Math.round(S.w * S.dpr); S.cv.height = Math.round(S.h * S.dpr);
  S.cx.setTransform(S.dpr, 0, 0, S.dpr, 0, 0);
  build();
  kick();
}

function kick() { if (S.on && !S.raf) S.raf = requestAnimationFrame(frame); }

const onMove = (e) => { S.tx = (e.clientX / window.innerWidth) * 2 - 1; S.ty = (e.clientY / window.innerHeight) * 2 - 1; };

export function setImmersive(on) {
  on = !!on;
  document.documentElement.classList.toggle('immersive', on);
  if (on === S.on) return;
  S.on = on;
  if (on) {
    S.cv = document.createElement('canvas');
    S.cv.id = 'immersive-bg';
    S.cv.setAttribute('aria-hidden', 'true');
    document.body.prepend(S.cv);
    S.cx = S.cv.getContext('2d');
    window.addEventListener('resize', resize);
    window.addEventListener('pointermove', onMove, { passive: true });
    document.addEventListener('visibilitychange', kick);
    S.themeObs = new MutationObserver(() => { if (S.night !== document.documentElement.classList.contains('dark')) { build(); kick(); if (S.reduced) frame(performance.now()); } });
    S.themeObs.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    resize();
    if (S.reduced) frame(performance.now());
  } else {
    cancelAnimationFrame(S.raf); S.raf = 0;
    window.removeEventListener('resize', resize);
    window.removeEventListener('pointermove', onMove);
    document.removeEventListener('visibilitychange', kick);
    S.themeObs?.disconnect();
    S.cv?.remove(); S.cv = null; S.cx = null;
  }
}

export function isImmersive() { return S.on; }
export function isQuest() { return /OculusBrowser|Quest|Pacific|Meta.*VR/i.test(navigator.userAgent); }
export function setComfort(on) { document.documentElement.classList.toggle('vr-comfort', !!on); }

// Auto-apply what the server rendered (logged-out pages / first paint).
if (document.documentElement.dataset.immersive === '1') setImmersive(true);
if (document.documentElement.dataset.vrComfort === '1' || (isQuest() && document.documentElement.dataset.vrComfort !== '0')) setComfort(true);
