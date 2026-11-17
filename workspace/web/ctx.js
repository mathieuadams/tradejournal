// Shared workspace object. app.js fills it in; the panels, the editor and the chat read and call it.
const listeners = {};
export const W = {
  state: null, chart: null, cfg: {}, account: null, quotes: {}, series: new Map(), indOut: [],
  on(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
  emit(ev, data) { for (const fn of listeners[ev] || []) { try { fn(data); } catch (e) { console.error(ev, e); } } },
};
