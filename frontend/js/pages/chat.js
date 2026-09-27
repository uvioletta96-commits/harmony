/**
 * Chat page: conversation list plus a message thread.
 *
 * Realtime is preferred, with a REST fallback. The thread re-requests history
 * on reconnect, because messages sent while the socket was down may exist on
 * the server but never have arrived over the wire.
 */

import { t } from '../core/i18n.js';

import api from '../core/api.js';
import { el, clear } from '../core/dom.js';
import { icon } from '../core/icons.js';
import store, { refreshCounters } from '../core/store.js';
import toast from '../core/toast.js';
import { router } from '../core/router.js';
import realtime from '../core/realtime.js';
import { relativeTime, smartDate, initialsOf, colourFor, dayLabel } from '../core/format.js';
import { avatar, button, emptyState, errorState, loadingRow, iconButton, setLoading } from '../components/ui.js';

const TYPING_TIMEOUT = 3500;

let teardown = [];

export async function render({ id } = {}) {
  teardown = [];
  const currentUser = store.get('currentUser');
  if (!currentUser) {
    return { node: emptyState({ iconName: 'lock', title: t('Войдите, чтобы открыть переписку') }) };
  }

  const listPanel = el('aside', { class: 'chat-list', id: 'chat-list' });
  const threadPanel = el('section', { class: 'chat-thread', id: 'chat-thread' });
  const shell = el('div', { class: 'chat-layout' }, listPanel, threadPanel);

  const active = id ? await openConversation(id, threadPanel, listPanel) : null;
  if (!active) threadPanel.style.display = 'none';
  listPanel.style.display = id ? 'none' : '';

  await loadConversations(listPanel, id);

  /* --- Realtime wiring -------------------------------------------------- */
  const offNew = realtime.on('message.new', (payload) => {
    if (!active) return;
    if (payload.conversation_id === active.id) {
      appendMessage(payload, { mine: payload.sender?.public_id === currentUser.public_id });
      realtime.markRead(active.id, payload.id);
      refreshCounters();
    } else {
      // Message in another thread: refresh the list preview.
      loadConversations(listPanel, id, { quiet: true });
    }
  });

  const offTyping = realtime.on('message:typing', (payload) => {
    if (!active || payload.conversation_id !== active.id) return;
    showTyping(payload.user, threadPanel);
  });

  const offConnected = realtime.on('connected', () => {
    if (!active) return;
    realtime.joinConversation(active.id);
    // Anything composed while offline may have been queued; re-read history.
    loadMessages(active, threadPanel, { silent: true });
  });

  teardown.push(offNew, offTyping, offConnected, realtime.connect);

  return {
    node: shell,
    unmount() {
      if (active) realtime.leaveConversation(active.id);
      teardown.forEach((fn) => fn?.());
      teardown = [];
    },
  };
}

/* -------------------------------------------------------------------------- */
/* Conversation list                                                           */
/* -------------------------------------------------------------------------- */

async function loadConversations(host, activeId, { quiet = false } = {}) {
  if (!quiet) host.append(loadingRow(t('Загружаем диалоги…')));
  try {
    const data = await api.get('/conversations');
    clear(host);

    if (!data.conversations.length) {
      host.append(
        emptyState({
          iconName: 'mail',
          title: t('Пока нет диалогов'),
          text: t('Найдите человека в поиске и напишите первым.'),
          action: button(t('Найти людей'), { variant: 'secondary', size: 'sm', onClick: () => router.navigate('/search') }),
        }),
      );
      return;
    }

    for (const conversation of data.conversations) {
      host.append(conversationRow(conversation, activeId));
    }
  } catch (error) {
    if (quiet) return;
    clear(host);
    host.append(errorState({ text: error.message, onRetry: () => loadConversations(host, activeId) }));
  }
}

function conversationRow(conversation, activeId) {
  const other = conversation.participants?.[0] || {};
  const unread = conversation.unread_count || 0;
  const isActive = conversation.id === activeId;

  return el('a', {
    class: `list-row ${unread ? 'list-unread' : ''}`,
    href: `/chat/${conversation.id}`,
    style: isActive ? { background: 'var(--bg-active)' } : {},
    'aria-current': isActive ? 'page' : undefined,
  },
    avatar(other, { size: 'md', link: false, showOnline: true }),
    el('div', { style: { minWidth: '0', flex: '1' } },
      el('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 'var(--space-2)' } },
        el('span', { class: 'list-row-title truncate', text: conversation.title || other.display_name || other.username || t('Диалог') }),
        el('span', { class: 'text-xs text-muted', style: { flexShrink: '0' }, text: conversation.last_message_at ? relativeTime(conversation.last_message_at) : '' }),
      ),
      el('div', { class: 'list-row-sub truncate', text: conversation.last_message_preview || t('Нет сообщений') }),
    ),
    unread ? el('span', { class: 'badge-count', text: String(unread) }) : null,
  );
}

/* -------------------------------------------------------------------------- */
/* Thread                                                                      */
/* -------------------------------------------------------------------------- */

async function openConversation(conversationId, host, listHost) {
  host.style.display = '';
  listHost.style.display = 'none';
  host.append(loadingRow(t('Открываем диалог…')));

  let conversation;
  try {
    conversation = (await api.get(`/conversations/${conversationId}`)).conversation;
  } catch (error) {
    clear(host);
    host.append(
      emptyState({
        iconName: 'alert',
        title: error.status === 404 ? t('Диалог не найден') : t('Не удалось открыть диалог'),
        text: error.message,
        action: button(t('К списку диалогов'), { variant: 'secondary', onClick: () => router.navigate('/chat') }),
      }),
    );
    return null;
  }

  clear(host);
  realtime.joinConversation(conversationId);

  const other = conversation.participants?.[0] || {};
  const messagesHost = el('div', { class: 'chat-messages', id: 'chat-messages' });
  const composerInput = el('textarea', {
    class: 'composer-input',
    rows: '1',
    maxlength: '4000',
    placeholder: t('Сообщение…'),
    'aria-label': t('Текст сообщения'),
  });
  const sendButton = iconButton('send', { title: t('Отправить') });

  const header = el('div', { class: 'chat-header' },
    el('button', { class: 'icon-btn md-hidden', type: 'button', 'aria-label': t('Назад к списку'), onclick: () => router.navigate('/chat') }, icon('chevronLeft', { size: 18 })),
    avatar(other, { size: 'sm', link: false, showOnline: true }),
    el('div', { style: { minWidth: '0', flex: '1' } },
      el('div', { class: 'list-row-title truncate', text: other.display_name || other.username || t('Диалог') }),
      other.username ? el('div', { class: 'list-row-sub', text: `@${other.username}` }) : null,
    ),
    el('button', {
      class: 'icon-btn',
      type: 'button',
      'aria-label': t('Открыть профиль'),
      title: t('Профиль'),
      onclick: () => other.username && router.navigate(`/u/${other.username}`),
    }, icon('user', { size: 18 })),
  );

  const composer = el('div', { class: 'chat-composer' },
    el('button', { class: 'icon-btn', type: 'button', 'aria-label': t('Прикрепить изображение'), title: t('Изображение'), onclick: () => attachImage(composerInput) }, icon('image', { size: 19 })),
    composerInput,
    sendButton,
  );

  host.append(header, messagesHost, composer);

  const context = { id: conversationId, other, messagesHost, composerInput, sendButton, typingNode: null, typingTimer: null };
  bindComposer(context);

  await loadMessages(context);

  return context;
}

async function loadMessages(context, { silent = false } = {}) {
  const { messagesHost } = context;
  if (!silent) messagesHost.append(loadingRow(t('Загружаем сообщения…')));

  try {
    const messages = await api.get(`/conversations/${context.id}/messages`);
    if (silent && messagesHost.children.length) return;
    clear(messagesHost);

    if (!messages.length) {
      messagesHost.append(
        emptyState({
          iconName: 'message',
          title: t('Начните разговор'),
          text: t('Напишите первым — {v0} увидит сообщение сразу.', { v0: context.other.display_name || context.other.username || 'собеседник' }),
        }),
      );
    } else {
      let lastDay = null;
      for (const message of messages) {
        const day = dayLabel(message.created_at);
        if (day !== lastDay) {
          messagesHost.append(el('div', { class: 'bubble-day', text: day }));
          lastDay = day;
        }
        messagesHost.append(bubble(message, false));
      }
    }
    scrollToBottom(messagesHost);
    realtime.markRead(context.id);
  } catch (error) {
    if (silent) return;
    clear(messagesHost);
    messagesHost.append(errorState({ text: error.message, onRetry: () => loadMessages(context) }));
  }
}

function bubble(message, mine) {
  const pending = message.__pending;
  const failed = message.__failed;

  const node = el('div', {
    class: `bubble ${mine ? 'bubble-out' : 'bubble-in'} ${pending ? 'bubble-pending' : ''} ${failed ? 'bubble-failed' : ''}`,
  },
    el('div', { text: message.body }),
    el('div', { class: 'bubble-meta' },
      smartDate(message.created_at),
      mine ? (failed ? t(' · не отправлено') : pending ? t(' · отправляется…') : '') : '',
    ),
  );
  return node;
}

function appendMessage(message, { mine }) {
  const host = document.getElementById('chat-messages');
  if (!host) return;
  const empty = host.querySelector('.empty');
  if (empty) empty.remove();

  const lastDay = [...host.querySelectorAll('.bubble-day')].pop()?.textContent;
  const day = dayLabel(message.created_at);
  if (day !== lastDay) host.append(el('div', { class: 'bubble-day', text: day }));

  const nearBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 120;
  host.append(bubble(message, mine));
  if (nearBottom || mine) scrollToBottom(host);
}

function scrollToBottom(host) {
  requestAnimationFrame(() => {
    host.scrollTop = host.scrollHeight;
  });
}

function bindComposer(context) {
  const { composerInput, sendButton } = context;

  const resize = () => {
    composerInput.style.height = 'auto';
    composerInput.style.height = `${Math.min(composerInput.scrollHeight, 140)}px`;
  };
  composerInput.addEventListener('input', () => {
    resize();
    realtime.sendTyping(context.id, composerInput.value.length > 0);
  });
  composerInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send();
    }
  });

  const send = () => {
    const body = composerInput.value.trim();
    if (!body) return;
    composerInput.value = '';
    resize();
    realtime.sendTyping(context.id, false);

    // Optimistic bubble: shown immediately, reconciled when the server acks.
    const optimistic = { body, created_at: new Date().toISOString(), __pending: true };
    appendMessage(optimistic, { mine: true });

    const offline = !realtime.isConnected();
    realtime.sendMessage(context.id, body);

    if (offline) {
      toast.info(t('Нет соединения — сообщение будет отправлено автоматически.'));
    }
  };

  sendButton.addEventListener('click', send);
  requestAnimationFrame(() => composerInput.focus());
}

async function attachImage(composerInput) {
  const input = el('input', { type: 'file', accept: 'image/jpeg,image/png,image/webp,image/gif', class: 'visually-hidden' });
  document.body.append(input);
  input.addEventListener('change', async () => {
    const file = input.files?.[0];
    input.remove();
    if (!file) return;
    if (file.size > 8 * 1024 * 1024) {
      toast.error(t('Файл больше 8 МБ'));
      return;
    }
    const body = new FormData();
    body.append('file', file);
    try {
      const result = await api.upload('/uploads/images', body);
      const url = result.files[0].thumbnail_url || result.files[0].url;
      composerInput.value += (composerInput.value ? '\n' : '') + url;
      composerInput.dispatchEvent(new Event('input'));
    } catch (error) {
      toast.error(error.message);
    }
  });
  input.click();
}

function showTyping(user, host) {
  if (host.querySelector('.typing-indicator')) return;
  const node = el('div', { class: 'typing-indicator', 'aria-label': t('{v0} печатает…', { v0: user?.display_name || 'Собеседник' }) },
    el('span'), el('span'), el('span'),
  );
  host.append(node);
  scrollToBottom(host);

  clearTimeout(showTyping.timer);
  showTyping.timer = setTimeout(() => node.remove(), TYPING_TIMEOUT);
}
