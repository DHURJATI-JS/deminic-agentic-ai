// Personal section - tabs (Tasks, Calendar, Family, Media, Summary) and chat card renderers.
import * as tasks from './personal/tasks.js';
import * as calendar from './personal/calendar.js';
import * as family from './personal/family.js';
import * as media from './personal/media.js';
import * as summary from './personal/summary.js';
import { registerPersonalCards } from './personal/cards.js';

export const tabs = [
  { id: 'tasks', label: 'Tasks', mount: tasks.mount, unmount: tasks.unmount },
  { id: 'calendar', label: 'Calendar', mount: calendar.mount, unmount: calendar.unmount },
  { id: 'family', label: 'Family', mount: family.mount, unmount: family.unmount },
  { id: 'media', label: 'Media', mount: media.mount, unmount: media.unmount },
  { id: 'summary', label: 'Summary', mount: summary.mount, unmount: summary.unmount },
];

export function registerCards(registerCard) {
  registerPersonalCards(registerCard);
}

export function init(ctx) {
  // Reminders are toasted by the shell; nothing else needs a global listener.
  return ctx;
}
