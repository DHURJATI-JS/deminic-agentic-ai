// Login (Google-like account chooser backed by Redis), registration wizard, forgot / reset password.
import { $, $$, api, bindThemeToggles, busy, esc, toast } from './core.js';
import { hydrate } from './icons.js';

bindThemeToggles();
hydrate(document);

// ------------------------------------------------------------------ login
async function initLogin() {
  if (new URLSearchParams(location.search).get('reason') === 'elsewhere') $('#elsewhere-note')?.classList.remove('hidden');
  const form = $('#login-form');
  const chooser = $('#chooser');
  const list = $('#account-list');
  const title = $('#login-title');
  const sub = $('#login-sub');
  const showForm = (acct) => {
    chooser.classList.add('hidden');
    form.classList.remove('hidden');
    $('#login-error').classList.add('hidden');
    if (acct) {
      $('#picked').classList.remove('hidden'); $('#picked').classList.add('flex');
      $('#picked-avatar').src = acct.avatar_url || '/static/img/avatar.svg';
      $('#picked-name').textContent = acct.name;
      $('#picked-user').textContent = `@${acct.username} · ${acct.email_masked || ''}`;
      $('#login').value = acct.username;
      $('#login-field').classList.add('hidden');
      title.textContent = `Welcome back, ${acct.name.split(' ')[0]}`;
      sub.textContent = 'Enter your password to continue';
      $('#password').focus();
    } else {
      $('#picked').classList.add('hidden'); $('#picked').classList.remove('flex');
      $('#login-field').classList.remove('hidden');
      $('#login').value = '';
      title.textContent = 'Sign in';
      sub.textContent = 'to continue to Deminic';
      $('#login').focus();
    }
  };
  const showChooser = () => { form.classList.add('hidden'); chooser.classList.remove('hidden'); title.textContent = 'Choose an account'; sub.textContent = 'saved on this device'; };

  let accounts = [];
  try { accounts = (await api('GET', '/api/auth/accounts')).accounts || []; } catch { accounts = []; }
  if (accounts.length) {
    list.innerHTML = '';
    for (const a of accounts) {
      const li = document.createElement('li');
      li.innerHTML = `<div class="flex items-center gap-3 rounded-2xl px-2 py-2 hover:bg-white/60 dark:hover:bg-white/[0.06] transition cursor-pointer" tabindex="0" role="button">
        <img src="${esc(a.avatar_url || '/static/img/avatar.svg')}" alt="" class="w-10 h-10 rounded-full object-cover bg-slate-200 dark:bg-slate-700">
        <div class="min-w-0 flex-1 text-left"><div class="text-sm font-medium truncate">${esc(a.name)}</div>
        <div class="text-xs text-slate-500 truncate">@${esc(a.username)} · ${esc(a.email_masked || '')}</div></div>
        ${a.signed_in ? '<span class="badge-green">signed in</span>' : ''}
        <button class="btn-plain btn-icon btn-sm remove" title="Remove from this device" aria-label="Remove account">&times;</button></div>`;
      const row = li.firstElementChild;
      row.addEventListener('click', (e) => {
        if (e.target.closest('.remove')) return;
        if (a.signed_in) { location.href = '/app'; return; }
        showForm(a);
      });
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter') row.click(); });
      row.querySelector('.remove').addEventListener('click', async (e) => {
        e.stopPropagation();
        await api('DELETE', `/api/auth/accounts/${encodeURIComponent(a.public_id)}`);
        li.remove();
        if (!list.children.length) showForm(null);
      });
      list.appendChild(li);
    }
    showChooser();
  } else {
    showForm(null);
  }
  $('#use-another').addEventListener('click', () => { showForm(null); $('#back-to-chooser').classList.toggle('hidden', !accounts.length); });
  $('#back-to-chooser').addEventListener('click', showChooser);
  if (accounts.length) $('#back-to-chooser').classList.remove('hidden');

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = $('#login-btn');
    const err = $('#login-error');
    err.classList.add('hidden');
    busy(btn, true);
    try {
      const r = await api('POST', '/api/auth/login', { login: $('#login').value.trim(), password: $('#password').value, remember: $('#remember').checked });
      location.href = r.redirect || '/app';
    } catch (ex) {
      err.textContent = ex.message; err.classList.remove('hidden');
      busy(btn, false);
    }
  });
}

// ------------------------------------------------------------------ register wizard
function initRegister() {
  const form = $('#register-form');
  const steps = $$('.wizard-step');
  let step = 0;
  try { $('#timezone').value = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch { /* ignore */ }

  const paint = () => {
    steps.forEach((s, i) => s.classList.toggle('hidden', i !== step));
    $$('#steps .step').forEach((li, i) => {
      li.querySelector('div > div').style.width = i < step ? '100%' : i === step ? '50%' : '0';
      li.querySelector('span').classList.toggle('text-accent-600', i === step);
    });
    $('#prev').classList.toggle('hidden', step === 0);
    $('#next').classList.toggle('hidden', step === steps.length - 1);
    $('#submit').classList.toggle('hidden', step !== steps.length - 1);
  };
  const fieldErr = (name, msg) => {
    const p = $(`[data-err="${name}"]`);
    if (p) { p.textContent = msg || ''; p.classList.toggle('hidden', !msg); }
  };
  const validate = () => {
    if (step !== 0) return true;
    let ok = true;
    const name = $('#name').value.trim(); fieldErr('name', name ? '' : 'Your name is required'); ok = ok && !!name;
    const u = $('#username').value.trim().toLowerCase();
    const uok = /^[a-z0-9_.-]{3,32}$/.test(u); fieldErr('username', uok ? '' : '3-32 chars: a-z 0-9 . _ -'); ok = ok && uok;
    const em = $('#email').value.trim(); const eok = /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(em); fieldErr('email', eok ? '' : 'A valid e-mail is required (used for password resets)'); ok = ok && eok;
    const pw = $('#password').value; const pok = pw.length >= 8 && /\d/.test(pw) && pw.toLowerCase() !== pw && pw.toUpperCase() !== pw;
    fieldErr('password', pok ? '' : '8+ chars with upper- and lower-case letters and a number'); ok = ok && pok;
    return ok;
  };
  $('#next').addEventListener('click', () => { if (validate()) { step = Math.min(step + 1, steps.length - 1); paint(); } });
  $('#prev').addEventListener('click', () => { step = Math.max(step - 1, 0); paint(); });
  $('#owns_business').addEventListener('change', (e) => {
    $('#biz-fields').classList.toggle('opacity-50', !e.target.checked);
    $('#biz-fields').classList.toggle('pointer-events-none', !e.target.checked);
  });
  $('#avatar').addEventListener('change', (e) => {
    const f = e.target.files[0];
    if (f) $('#avatar-preview').src = URL.createObjectURL(f);
  });
  let t;
  $('#username').addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(async () => {
      const u = $('#username').value.trim().toLowerCase();
      if (u.length < 3) return;
      try {
        const r = await api('GET', `/api/auth/check-username?u=${encodeURIComponent(u)}`);
        $('#username-hint').textContent = r.ok ? '✓ available' : r.message;
        $('#username-hint').className = r.ok ? 'hint text-emerald-600' : 'hint text-rose-600';
      } catch { /* ignore */ }
    }, 350);
  });
  form.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target.tagName === 'INPUT' && step < steps.length - 1) { e.preventDefault(); $('#next').click(); } });
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = $('#submit');
    const err = $('#register-error');
    err.classList.add('hidden');
    const fd = new FormData(form);
    if (!$('#owns_business').checked) fd.set('owns_business', 'false');
    if (!$('#avatar').files.length) fd.delete('avatar');
    busy(btn, true, 'Creating');
    try {
      const r = await api('POST', '/api/auth/register', fd);
      toast('Account created - welcome!', 'ok');
      location.href = r.redirect || '/app';
    } catch (ex) {
      busy(btn, false);
      const field = ex.data && ex.data.field;
      if (field) {
        const idx = { name: 0, username: 0, email: 0, password: 0 }[field];
        if (idx !== undefined) { step = idx; paint(); fieldErr(field, ex.message); return; }
      }
      err.textContent = ex.message; err.classList.remove('hidden');
    }
  });
  paint();
}

// ------------------------------------------------------------------ forgot / reset
function initForgot() {
  $('#forgot-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const msg = $('#forgot-msg');
    try {
      const r = await api('POST', '/api/auth/forgot', { email: $('#email').value.trim() });
      msg.textContent = r.message; msg.className = 'text-sm text-emerald-600';
    } catch (ex) { msg.textContent = ex.message; msg.className = 'text-sm text-rose-600'; }
  });
}

function initReset() {
  $('#reset-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const msg = $('#reset-msg');
    if ($('#password').value !== $('#password2').value) { msg.textContent = 'Passwords do not match'; msg.className = 'text-sm text-rose-600'; return; }
    try {
      await api('POST', '/api/auth/reset', { token: $('#token').value, password: $('#password').value });
      msg.textContent = 'Password saved - redirecting to sign in...'; msg.className = 'text-sm text-emerald-600';
      setTimeout(() => (location.href = '/login'), 1200);
    } catch (ex) { msg.textContent = ex.message; msg.className = 'text-sm text-rose-600'; }
  });
}

if ($('#login-form')) initLogin();
if ($('#register-form')) initRegister();
if ($('#forgot-form')) initForgot();
if ($('#reset-form')) initReset();
