export const store = {
  sessions: [],
  kbs: [],
  activeSessionId: null,
  activeKbId: null,
  settings: {},
  health: null,
  ready: false,
};

const listeners = new Map();

export function on(event, callback) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(callback);
  return () => listeners.get(event)?.delete(callback);
}

export function emit(event, data) {
  for (const callback of listeners.get(event) || []) {
    try {
      callback(data);
    } catch (error) {
      console.error(`listener for ${event} failed`, error);
    }
  }
}
