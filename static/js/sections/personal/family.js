// Family tab - family-member profiles (stored in global memory) and the
// e-mails the AI drafted / sent to them through the user's SMTP.
import { $, api, busy, confirmModal, debounce, modal, toast } from '../../core.js';
import { esc, flash, fmtRel, formNode, formValues, initials, refLink, STATUS_BADGE } from './util.js';

let root = null;
let C = null;
let contacts = [];
let smtp = false;
const unsubs = [];
const RELATIONS = ['mother', 'father', 'brother', 'sister', 'wife', 'husband', 'partner', 'son', 'daughter', 'grandmother',
  'grandfather', 'uncle', 'aunt', 'cousin', 'friend', 'colleague', 'other'];
const GRADS = ['from-sky-400 to-accent-500', 'from-fuchsia-400 to-rose-500', 'from-amber-400 to-orange-500', 'from-emerald-400 to-teal-500', 'from-violet-400 to-indigo-500'];

export async function mount(el, ctx) {
  root = el; C = ctx;
  el.innerHTML = `<div class="h-full overflow-y-auto scroll-thin p-5 md:p-8">
    <div class="flex flex-wrap items-center gap-3">
      <div><h2 class="text-xl font-semibold">Family &amp; contacts</h2>
        <p class="text-sm text-slate-500">Profiles live in your personal global memory. Ask ${esc(ctx.me.user.ai_name)} to e-mail them - it drafts, you press Send.</p></div>
      <span class="flex-1"></span>
      <button class="btn-primary" id="f-add">Add family member</button>
    </div>
    <div id="f-smtp" class="hidden mt-4 card !bg-amber-500/10 text-sm">SMTP isn't configured yet - drafts are saved but can't be sent. <a href="/settings#smtp">Set up e-mail</a></div>
    <div id="f-grid" class="mt-5 grid sm:grid-cols-2 xl:grid-cols-3 gap-3"></div>
    <div class="mt-8 flex items-center gap-2"><h3 class="font-semibold">E-mails</h3><span class="chip" id="f-ecount">0</span></div>
    <div id="f-emails" class="mt-3 space-y-2"></div></div>`;
  $('#f-add', el).addEventListener('click', () => editContact(null));
  const reload = debounce(load, 300);
  unsubs.push(ctx.on('memory:changed', (d) => { if (d.section === 'personal') reload(); }));
  unsubs.push(ctx.on('email:changed', reload));
  unsubs.push(ctx.on('ref:focus', async (r) => { if (r.type === 'email') { await load(); flash(root && root.querySelector(`[data-email-no="${r.no}"]`)); } }));
  await load();
}

export function unmount() { while (unsubs.length) unsubs.pop()(); root = null; }

async function load() {
  if (!root) return;
  try {
    const [c, e] = await Promise.all([api('GET', '/api/personal/contacts'), api('GET', '/api/personal/emails')]);
    contacts = c.contacts; smtp = e.smtp;
    paintContacts();
    paintEmails(e.emails);
    $('#f-smtp', root).classList.toggle('hidden', smtp);
  } catch (ex) { toast(ex.message, 'error'); }
}

function paintContacts() {
  const grid = $('#f-grid', root);
  grid.innerHTML = contacts.length ? '' : '<div class="card text-sm text-slate-500 sm:col-span-2 xl:col-span-3 text-center py-10">No family profiles yet. Try: <i>"create a profile for my brother Rahul, rahul@example.com"</i></div>';
  contacts.forEach((c, i) => {
    const card = document.createElement('div');
    card.className = 'card flex gap-3 items-start';
    card.innerHTML = `<div class="w-12 h-12 shrink-0 rounded-2xl bg-gradient-to-br ${GRADS[i % GRADS.length]} text-white font-semibold flex items-center justify-center">${esc(initials(c.name))}</div>
      <div class="min-w-0 flex-1"><div class="font-medium truncate">${esc(c.name)}</div>
        <div class="text-xs text-slate-500 capitalize">${esc(c.relation || 'contact')}</div>
        <div class="mt-1.5 space-y-0.5 text-xs text-slate-600 dark:text-slate-300 break-all">
          ${c.email ? `<div>✉️ ${esc(c.email)}</div>` : '<div class="text-slate-400">no e-mail</div>'}
          ${c.phone ? `<div>📞 ${esc(c.phone)}</div>` : ''}${c.birthday ? `<div>🎂 ${esc(c.birthday)}</div>` : ''}
          ${c.notes ? `<div class="text-slate-500 break-words">${esc(c.notes)}</div>` : ''}
          ${c.source_ref ? `<div>from ${refLink(c.source_ref)}</div>` : ''}</div>
        <div class="mt-2 flex flex-wrap gap-1"><button class="btn-ghost btn-sm" data-act="mail" ${c.email ? '' : 'disabled'}>E-mail</button>
          <button class="btn-plain btn-sm" data-act="edit">Edit</button><button class="btn-plain btn-sm text-rose-600" data-act="del">Delete</button></div></div>`;
    card.addEventListener('click', async (e) => {
      const b = e.target.closest('[data-act]');
      if (!b) return;
      if (b.dataset.act === 'edit') editContact(c);
      else if (b.dataset.act === 'mail') compose(c);
      else if (b.dataset.act === 'del') {
        if (!(await confirmModal('Delete profile?', `${c.name} will be removed from your global memory.`, { danger: true, confirmText: 'Delete' }))) return;
        await api('DELETE', `/api/personal/contacts/${c.id}`);
        toast('Profile deleted', 'ok');
        load();
      }
    });
    grid.appendChild(card);
  });
}

async function editContact(c) {
  const form = formNode([
    { name: 'name', label: 'Name', value: c?.name || '', required: true },
    { name: 'relation', label: 'Relation', type: 'select', value: c?.relation || 'other', options: RELATIONS },
    { name: 'email', label: 'E-mail', type: 'email', value: c?.email || '' },
    { name: 'phone', label: 'Phone', value: c?.phone || '' },
    { name: 'birthday', label: 'Birthday', value: c?.birthday || '', placeholder: 'e.g. 4 May' },
    { name: 'notes', label: 'Notes', type: 'textarea', value: c?.notes || '', rows: 3 },
  ]);
  const ok = await modal({ title: c ? `Edit ${c.name}` : 'Add family member', body: form, actions: [{ label: 'Cancel', value: false }, { label: 'Save', kind: 'primary', value: true }] });
  if (!ok) return;
  const v = formValues(form);
  if (!v.name.trim()) { toast('A name is required', 'warn'); return; }
  try {
    if (c) await api('PATCH', `/api/personal/contacts/${c.id}`, v);
    else await api('POST', '/api/personal/contacts', v);
    toast('Profile saved to global memory', 'ok');
    load();
  } catch (ex) { toast(ex.message, 'error'); }
}

async function compose(c) {
  const box = document.createElement('div');
  box.innerHTML = `<p class="text-xs text-slate-500">To ${esc(c.name)} &lt;${esc(c.email)}&gt;</p>
    <label class="label mt-3" for="cmp-purpose">What should it say? (AI draft)</label>
    <div class="flex gap-2"><input class="input" id="cmp-purpose" placeholder="e.g. I'll be late for dinner tonight"><button type="button" class="btn-ghost" id="cmp-ai">Draft with AI</button></div>
    <label class="label mt-3" for="cmp-subject">Subject</label><input class="input" id="cmp-subject">
    <label class="label mt-3" for="cmp-body">Message</label><textarea class="textarea" id="cmp-body" rows="7"></textarea>`;
  box.querySelector('#cmp-ai').addEventListener('click', async (e) => {
    const purpose = box.querySelector('#cmp-purpose').value.trim();
    if (!purpose) { toast('Describe what the e-mail is about', 'warn'); return; }
    busy(e.target, true);
    try {
      const r = await api('POST', '/api/personal/emails/draft-ai', { contact_id: c.id, purpose });
      box.querySelector('#cmp-subject').value = r.email.subject;
      box.querySelector('#cmp-body').value = r.email.body;
      box.dataset.emailNo = r.email.email_no;
      toast(`Draft saved as ${r.email.ref}`, 'ok');
    } catch (ex) { toast(ex.message, 'error'); } finally { busy(e.target, false); }
  });
  const act = await modal({ title: `E-mail ${c.name}`, body: box, wide: true, actions: [{ label: 'Cancel', value: false }, { label: 'Save draft', value: 'save' }, { label: 'Send', kind: 'primary', value: 'send' }] });
  if (!act) { load(); return; }
  const subject = box.querySelector('#cmp-subject').value.trim();
  const body = box.querySelector('#cmp-body').value;
  try {
    let no = Number(box.dataset.emailNo || 0);
    if (no) await api('PATCH', `/api/personal/emails/${no}`, { subject, body });
    else no = (await api('POST', '/api/personal/emails', { contact_id: c.id, subject, body })).email.email_no;
    if (act === 'send') await send(no);
    else toast(`Draft email#${no} saved`, 'ok');
  } catch (ex) { toast(ex.message, 'error'); }
  load();
}

export async function send(no) {
  try {
    const r = await api('POST', `/api/personal/emails/${no}/send`);
    toast(r.ok ? r.message || 'Sent' : r.message, r.ok ? 'ok' : 'error', 7000);
    return r;
  } catch (ex) { toast(ex.message, 'error', 7000); return null; }
}

function paintEmails(list) {
  const box = $('#f-emails', root);
  $('#f-ecount', root).textContent = list.length;
  box.innerHTML = list.length ? '' : '<p class="text-sm text-slate-500">No e-mails yet.</p>';
  for (const m of list) {
    const div = document.createElement('div');
    div.className = 'card !p-3 transition';
    div.dataset.emailNo = m.email_no;
    div.innerHTML = `<div class="flex flex-wrap items-center gap-2"><span class="font-medium break-words">${esc(m.subject || '(no subject)')}</span>
        <span class="${STATUS_BADGE[m.status] || 'badge-gray'}">${esc(m.status)}</span><span class="chip !h-5">${esc(m.kind)}</span>
        <span class="flex-1"></span>
        ${m.status !== 'sent' ? '<button class="btn-primary btn-sm" data-act="send">Send</button>' : ''}
        <button class="btn-plain btn-sm" data-act="view">${m.status === 'sent' ? 'View' : 'Edit'}</button>
        <button class="btn-plain btn-sm text-rose-600" data-act="del">Delete</button></div>
      <div class="mt-1 text-[11px] text-slate-500 flex flex-wrap gap-x-3">to ${esc(m.to_name || '')} &lt;${esc(m.to_email)}&gt; · ${refLink(m.ref)}
        ${m.source_ref ? '· from ' + refLink(m.source_ref) : ''} · ${esc(fmtRel(m.sent_at || m.created_at))}</div>
      ${m.error ? `<div class="mt-1 text-xs text-rose-600 break-words">${esc(m.error)}</div>` : ''}
      <div class="mt-1.5 text-xs text-slate-600 dark:text-slate-300 line-clamp-2 whitespace-pre-wrap break-words">${esc(m.body.slice(0, 300))}</div>`;
    div.addEventListener('click', async (e) => {
      const b = e.target.closest('[data-act]');
      if (!b) return;
      if (b.dataset.act === 'send') { busy(b, true); await send(m.email_no); load(); }
      else if (b.dataset.act === 'del') {
        if (!(await confirmModal('Delete e-mail?', `${m.ref} will be deleted from your outbox.`, { danger: true, confirmText: 'Delete' }))) return;
        await api('DELETE', `/api/personal/emails/${m.email_no}`); load();
      } else if (b.dataset.act === 'view') editEmail(m);
    });
    box.appendChild(div);
  }
}

export async function editEmail(m, after) {
  const readonly = m.status === 'sent';
  const form = formNode([
    { name: 'to_email', label: 'To', value: m.to_email },
    { name: 'to_name', label: 'Name', value: m.to_name },
    { name: 'subject', label: 'Subject', value: m.subject, full: true },
    { name: 'body', label: 'Message', type: 'textarea', value: m.body, rows: 9 },
  ]);
  if (readonly) for (const x of form.elements) x.disabled = true;
  const act = await modal({ title: `${m.ref}${readonly ? ' (sent)' : ''}`, body: form, wide: true,
    actions: readonly ? [{ label: 'Close', value: false }] : [{ label: 'Cancel', value: false }, { label: 'Save', value: 'save' }, { label: 'Save & send', kind: 'primary', value: 'send' }] });
  if (!act) return;
  try {
    await api('PATCH', `/api/personal/emails/${m.email_no}`, formValues(form));
    if (act === 'send') await send(m.email_no); else toast('Saved', 'ok');
  } catch (ex) { toast(ex.message, 'error'); }
  if (after) after(); else load();
}
