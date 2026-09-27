/**
 * Socket.IO client with automatic reconnection.
 *
 * Beyond reconnecting, this module also solves the two problems that make
 * realtime features feel unreliable in practice:
 *
 *  1. **Outbox** — messages composed while the socket is down are queued and
 *     flushed on reconnect, so a user never loses what they typed because the
 *     train went into a tunnel.
 *  2. **Ordering** — a small receive sequence guard drops out-of-order or
 *     duplicated frames that survive a reconnect.
 */

import { config } from '../config.js';

const URL = config.socketUrl;
const NAMESPACES = ['/'];

let socket = null;
let connected = false;
let reconnectAttempts = 0;
let reconnectTimer = null;
const outbox = [];
const handlers = new Map();
let lastSeq = 0;

const RECONNECT_BASE = 800;
const RECONNECT_MAX = 15000;

export function connect() {
  if (socket) return socket;

  // Loaded lazily so the rest of the app works even if the socket bundle fails.
  const io = window.io;
  if (!io) {
    console.warn('[realtime] Socket.IO client not loaded; falling back to REST');
    return null;
  }

  socket = io(URL, {
    path: '/socket.io',
    transports: ['websocket', 'polling'],
    withCredentials: true,
    reconnection: true,
    reconnectionDelay: RECONNECT_BASE,
    reconnectionDelayMax: RECONNECT_MAX,
    randomizationFactor: 0.5,
    timeout: 12000,
  });

  socket.on('connect', () => {
    connected = true;
    reconnectAttempts = 0;
    emit('connected', { reconnectAttempts: 0 });
    flushOutbox();
  });

  socket.on('disconnect', (reason) => {
    connected = false;
    emit('disconnected', { reason });
  });

  socket.on('connect_error', (error) => {
    reconnectAttempts += 1;
    emit('connect_error', { error, attempts: reconnectAttempts });
  });

  // Server-sent events -------------------------------------------------
  for (const event of ['connected', 'post.new', 'comment.new', 'message.new', 'message.typing', 'message.deleted', 'error:notice']) {
    socket.on(event, (payload) => {
      if (event === 'message.new' && payload?.seq !== undefined) {
        if (payload.seq <= lastSeq) return; // duplicate after a reconnect
        lastSeq = payload.seq;
      }
      emit(event, payload);
    });
  }

  return socket;
}

export function disconnect() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  socket?.disconnect();
  socket = null;
  connected = false;
}

export function isConnected() {
  return Boolean(socket?.connected);
}

/** Subscribe to a socket event. Returns an unsubscribe function. */
export function on(event, handler) {
  if (!handlers.has(event)) handlers.set(event, new Set());
  handlers.get(event).add(handler);
  return () => handlers.get(event)?.delete(handler);
}

function emit(event, payload) {
  for (const handler of handlers.get(event) || []) {
    try {
      handler(payload);
    } catch (error) {
      console.error(`[realtime] handler for "${event}" threw`, error);
    }
  }
}

/**
 * Emit with an acknowledgement and a timeout.
 * Resolves with the server's ack, or rejects if the socket is unavailable.
 */
export function request(event, payload, { timeoutMs = 10000 } = {}) {
  return new Promise((resolve, reject) => {
    if (!socket?.connected) {
      reject(new Error('offline'));
      return;
    }
    socket.timeout(timeoutMs).emit(event, payload, (error, response) => {
      if (error) reject(new Error('timeout'));
      else resolve(response);
    });
  });
}

/** Send a message, queueing it when offline. */
export function sendMessage(conversationId, body) {
  const frame = { event: 'message:send', payload: { conversation_id: conversationId, body } };

  if (socket?.connected) {
    socket.emit(frame.event, frame.payload, (ack) => {
      if (ack?.ok === false) emit('message.rejected', ack);
    });
    return;
  }
  outbox.push(frame);
}

async function flushOutbox() {
  while (outbox.length) {
    const frame = outbox.shift();
    try {
      const ack = await request(frame.event, frame.payload, { timeoutMs: 8000 });
      if (ack?.ok === false) emit('message.rejected', { ...ack, queued: frame.payload.body });
    } catch {
      // Still failing: put it back at the front and stop for this cycle.
      outbox.unshift(frame);
      break;
    }
  }
}

export function joinConversation(conversationId) {
  if (socket?.connected) socket.emit('conversation:join', { conversation_id: conversationId });
}

export function leaveConversation(conversationId) {
  if (socket?.connected) socket.emit('conversation:leave', { conversation_id: conversationId });
}

export function markRead(conversationId, upToMessageId = null) {
  if (socket?.connected) {
    socket.emit('conversation:read', { conversation_id: conversationId, up_to_message_id: upToMessageId });
  }
}

let typingThrottle = 0;
export function sendTyping(conversationId, isTyping = true) {
  if (!socket?.connected) return;
  // Throttle: emitting on every keystroke would flood the room.
  const now = Date.now();
  if (now - typingThrottle < 2000) return;
  typingThrottle = now;
  socket.emit('message:typing', { conversation_id: conversationId, is_typing: isTyping });
}

export const realtime = {
  connect,
  disconnect,
  on,
  request,
  sendMessage,
  joinConversation,
  leaveConversation,
  markRead,
  sendTyping,
  isConnected,
};

export default realtime;
