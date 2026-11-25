// Small shared helpers for the personal section modules.
import { el, esc, fmtDate, fmtRel } from '../../core.js';

export { el, esc, fmtDate, fmtRel };

export function toLocalInput(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function fromLocalInput(v) {
  if (!v) return null;
  const d = new Date(v);
  return isNaN(d) ? null : d.toISOString();
}

export function initials(name) {
  return String(name || '?').split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join('') || '?';
}

export function flash(node) {
  if (!node) return;
  node.scrollIntoView({ behavior: 'smooth', block: 'center' });
  node.classList.add('ring-2', 'ring-accent-500/60');
  setTimeout(() => node.classList.remove('ring-2', 'ring-accent-500/60'), 2600);
}

export function refLink(ref) {
  return ref ? `<a class="ref-link" data-ref="${esc(ref)}">${esc(ref)}</a>` : '';
}

export function sameDay(a, b) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

export function timeOnly(iso) {
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
}

export const STATUS_BADGE = {
  active: 'badge-blue', queued: 'badge-violet', done: 'badge-green', cancelled: 'badge-gray',
  draft: 'badge-amber', sent: 'badge-green', failed: 'badge-red',
};

/** Simple form modal: returns {values} or null. fields: [{name,label,type,value,placeholder,options,required,full}] */
export function formNode(fields) {
  const wrap = document.createElement('form');
  wrap.className = 'grid sm:grid-cols-2 gap-3 text-left';
  wrap.noValidate = true;
  for (const f of fields) {
    const box = document.createElement('div');
    if (f.full || f.type === 'textarea') box.className = 'sm:col-span-2';
    const id = `f-${f.name}-${Math.random().toString(36).slice(2, 7)}`;
    let input;
    if (f.type === 'textarea') input = `<textarea class="textarea" id="${id}" name="${esc(f.name)}" rows="${f.rows || 4}" placeholder="${esc(f.placeholder || '')}">${esc(f.value || '')}</textarea>`;
    else if (f.type === 'select') input = `<select class="select" id="${id}" name="${esc(f.name)}">${(f.options || []).map((o) => `<option value="${esc(o.value ?? o)}" ${String(o.value ?? o) === String(f.value ?? '') ? 'selected' : ''}>${esc(o.label ?? o)}</option>`).join('')}</select>`;
    else if (f.type === 'checkbox') input = `<label class="flex items-center gap-2 text-sm mt-6"><input type="checkbox" id="${id}" name="${esc(f.name)}" ${f.value ? 'checked' : ''}> ${esc(f.label)}</label>`;
    else input = `<input class="input" id="${id}" name="${esc(f.name)}" type="${esc(f.type || 'text')}" value="${esc(f.value ?? '')}" placeholder="${esc(f.placeholder || '')}" ${f.required ? 'required' : ''} autocomplete="off">`;
    box.innerHTML = f.type === 'checkbox' ? input : `<label class="label" for="${id}">${esc(f.label)}</label>${input}${f.hint ? `<p class="hint">${esc(f.hint)}</p>` : ''}`;
    wrap.appendChild(box);
  }
  return wrap;
}

export function formValues(form) {
  const out = {};
  for (const elx of form.elements) {
    if (!elx.name) continue;
    out[elx.name] = elx.type === 'checkbox' ? elx.checked : elx.value;
  }
  return out;
}
