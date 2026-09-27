/**
 * Global application state.
 *
 * A tiny observable store rather than a framework. Pages subscribe to the
 * slices they care about and re-render only when those slices change.
 */

import api, { ApiError, setUnauthorizedHandler } from './api.js';
import { bindStore as bindDrafts } from './drafts.js';

const listeners = new Map();
let nextId = 1;

const state = {
  currentUser: null,
  authResolved: false,
  notifications: { unread: 0, messages: 0 },
  connection: 'offline',   // offline | connecting | online
  theme: localStorage.getItem('harmony:theme') || 'light',
};

export function subscribe(key, handler) {
  const id = nextId++;
  if (!listeners.has(key)) listeners.set(key, new Map());
  listeners.get(key).set(id, handler);
  return () => listeners.get(key)?.delete(id);
}

export function emit(key, value) {
  const handlers = listeners.get(key);
  if (!handlers) return;
  for (const handler of handlers.values()) {
    try {
      handler(value);
    } catch (error) {
      console.error(`[store] listener for "${key}" threw`, error);
    }
  }
}

export function get(key) {
  return state[key];
}

export function set(patch) {
  for (const [key, value] of Object.entries(patch)) {
    state[key] = value;
    emit(key, value);
    emit('*', { key, value });
  }
}

// The drafts module needs to know who is writing, and it cannot import the store
// back without a cycle. Bound once, here, where both exist.
bindDrafts({ get: (key) => state[key] });

export function isAuthenticated() {
  return Boolean(state.currentUser);
}

export function isModerator() {
  const user = state.currentUser;
  return Boolean(user && (user.role === 'moderator' || user.role === 'admin'));
}

export function isAdmin() {
  return state.currentUser?.role === 'admin';
}

/** Resolve the current session once, at boot. */
export async function resolveSession() {
  try {
    const data = await api.get('/auth/me');
    set({ currentUser: data.user, authResolved: true });
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      set({ currentUser: null, authResolved: true });
    } else {
      // Network or server problem: stay unresolved but do not crash the boot.
      set({ authResolved: true, currentUser: null });
      throw error;
    }
  }
  return state.currentUser;
}

export async function login(identifier, password) {
  const data = await api.post('/auth/login', { identifier, password });
  set({ currentUser: data.user, authResolved: true });
  return data.user;
}

export async function logout() {
  try {
    await api.logout();
  } finally {
    set({ currentUser: null });
    emit('logged-out');
  }
}

export function patchCurrentUser(patch) {
  if (!state.currentUser) return;
  set({ currentUser: { ...state.currentUser, ...patch } });
}

/* -------------------------------------------------------------------------- */
/* Notifications                                                               */
/* -------------------------------------------------------------------------- */

let notificationTimer = null;

export async function refreshCounters() {
  if (!isAuthenticated()) return;
  try {
    const [notifications, chat] = await Promise.all([
      api.get('/notifications/unread-count'),
      api.get('/messages/unread-count'),
    ]);
    set({
      notifications: {
        unread: notifications.unread_count ?? 0,
        messages: chat.unread ?? 0,
      },
    });
  } catch {
    /* counters are cosmetic; a failure must not surface to the user */
  }
}

export function startCounterPolling(intervalMs = 45000) {
  stopCounterPolling();
  refreshCounters();
  notificationTimer = setInterval(refreshCounters, intervalMs);
  document.addEventListener('visibilitychange', onVisibility);
  return stopCounterPolling;
}

export function stopCounterPolling() {
  if (notificationTimer) clearInterval(notificationTimer);
  notificationTimer = null;
  document.removeEventListener('visibilitychange', onVisibility);
}

function onVisibility() {
  // Pause polling in a background tab; resume and catch up on return.
  if (document.hidden) {
    stopCounterPolling();
  } else {
    startCounterPolling();
  }
}

/* -------------------------------------------------------------------------- */
/* Cross-cutting wiring                                                        */
/* -------------------------------------------------------------------------- */

setUnauthorizedHandler(() => {
  if (state.currentUser) {
    set({ currentUser: null });
    emit('logged-out');
  }
});

window.addEventListener('online', () => set({ connection: 'online' }));
window.addEventListener('offline', () => set({ connection: 'offline' }));
set({ connection: navigator.onLine ? 'online' : 'offline' });

/* -------------------------------------------------------------------------- */
/* Default export                                                              */
/* -------------------------------------------------------------------------- */

/**
 * Every page imports the store as a single object, while the functions are also
 * available individually for pages that only need one of them. Two spellings of
 * one API is a deliberate choice here, not an accident: `store.get('currentUser')`
 * reads better at the call site than a bare `get('currentUser')` that looks
 * like an array lookup.
 */
const store = {
  get,
  set,
  emit,
  subscribe,
  isAuthenticated,
  isModerator,
  isAdmin,
  resolveSession,
  login,
  logout,
  patchCurrentUser,
  refreshCounters,
  startCounterPolling,
  stopCounterPolling,
  get state() {
    return { ...state };
  },
};

export default store;
