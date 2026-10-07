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
import { avatar, button, emptyState, errorState, loadingRow, iconButton, openLightbox, setLoading } from '../components/ui.js';
import {
  canRecord,
  messageAttachments,
  pickAndUpload,
  startRecording,
  uploadRecording,
} from '../components/chatMedia.js';

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

      // The microphone first, before anything else is torn down.
      //
      // A recording left running after leaving the thread is a microphone indicator
      // that never goes away, and the reader has no way to stop it - the button
      // that stops it has just left the document. `cancel` rather than `stop`, so
      // nothing is recorded into the void and then uploaded.
      active?.composer?.stopRecording();

      // Pause anything playing. An <audio> that keeps playing after the thread is
      // gone keeps pulling bytes over a mobile connection for audio nobody can
      // reach the control for.
      for (const media of document.querySelectorAll('.chat-messages audio, .chat-messages video')) {
        media.pause?.();
      }

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

  const context = {
    id: conversationId,
    other,
    messagesHost,
    composerInput,
    sendButton,
    typingNode: null,
    typingTimer: null,
  };

  const composer = buildComposer(context);
  context.composer = composer;

  host.append(header, messagesHost, composer.node);

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

  const attachments = messageAttachments(message, {
    onOpen: (attachment) => openLightbox({ ...attachment, url: attachment.url }),
  });

  const node = el('div', {
    class: `bubble ${mine ? 'bubble-out' : 'bubble-in'} ${pending ? 'bubble-pending' : ''} ${failed ? 'bubble-failed' : ''} ${attachments ? 'bubble-has-media' : ''}`,
  },
    attachments,
    // Only when there is text. An empty div for a photo-only message puts a gap in
    // the bubble and a stray set of line breaks under the picture.
    message.body ? el('div', { class: 'bubble-text', text: message.body }) : null,
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

/**
 * The composer's file and recording controls.
 *
 * The tray sits above the text field and holds whatever has been picked but not
 * yet sent, each with its own remove button. It is a tray rather than a
 * send-immediately list because a photo picked and a sentence typed over it are one
 * message, and making the reader choose an order before they have finished typing
 * is a worse question than "send".
 *
 * Recording is hold-to-record. The microphone is open only while a finger is
 * down, so there is no recording left running after the reader lets go and nothing
 * to send by accident.
 */
function buildComposer(context) {
  const { composerInput, sendButton } = context;

  const tray = el('div', { class: 'composer-tray', hidden: true });
  const pending = [];

  /**
   * Redraw the tray from `pending`.
   *
   * One source of truth: the array is the list, and the row of chips is rebuilt
   * from it. The earlier version removed a node and then re-appended it, which
   * meant the chip a reader had just removed came straight back.
   */
  const paintTray = () => {
    clear(tray);
    tray.hidden = pending.length === 0;
    tray.dataset.count = String(pending.length);
    if (!pending.length) return;

    tray.append(el('div', { class: 'composer-chips' }, ...pending.map(chipFor)));
  };

  const addPending = (attachment) => {
    pending.push(attachment);
    paintTray();
    syncSend();
  };

  /** One removable chip, rebuilt from an attachment each time the tray is drawn. */
  function chipFor(attachment) {
    const chip = attachment.kind === 'voice' || attachment.kind === 'circle'
      ? el('span', { class: 'composer-chip composer-chip-voice' },
          icon('mic', { size: 15 }),
          el('span', { text: attachment.kind === 'circle' ? t('Кружок') : t('Голосовое') }))
      : attachment.kind === 'video'
        ? el('span', { class: 'composer-chip' }, icon('video', { size: 15 }), el('span', { text: t('Видео') }))
        : el('img', {
            class: 'composer-chip-image',
            src: attachment.thumbnail_url || attachment.url,
            alt: t('Прикреплённое фото'),
          });

    const remove = el('button', {
      class: 'composer-chip-remove',
      type: 'button',
      'aria-label': t('Убрать'),
      title: t('Убрать'),
    }, icon('close', { size: 13 }));

    remove.addEventListener('click', () => {
      const index = pending.indexOf(attachment);
      if (index >= 0) pending.splice(index, 1);
      // The file was already uploaded, so removing the chip stops it being a
      // candidate rather than un-uploading it: there is no endpoint to un-claim it
      // with, and the nightly sweep collects the row.
      paintTray();
      syncSend();
    });

    return el('span', { class: 'composer-chip-wrap' }, chip, remove);
  }

  const syncSend = () => {
    // A message may be only files, only text, or both. The button follows all three
    // rather than only the first, or a photo-only message is unsendable.
    sendButton.classList.toggle('is-ready', Boolean(composerInput.value.trim()) || pending.length > 0);
    sendButton.disabled = !composerInput.value.trim() && pending.length === 0;
  };

  const pickPhotos = async () => {
    const picked = await pickAndUpload(
      context.id,
      'image/jpeg,image/png,image/webp,image/gif',
      'image',
    );
    for (const attachment of picked) addPending(attachment);
  };

  const pickVideos = async () => {
    const picked = await pickAndUpload(context.id, 'video/mp4,video/webm,video/ogg', 'video');
    for (const attachment of picked) addPending(attachment);
  };

  /* -- Recording --------------------------------------------------------- */

  let recording = null;
  let circleMode = false;

  const recordButton = el('button', {
    class: 'composer-mic',
    type: 'button',
    'aria-label': t('Записать голосовое сообщение'),
    title: t('Записать голосовое'),
  },
    icon('mic', { size: 19 }),
    el('span', { class: 'composer-mic-time' }),
  );

  const circleButton = el('button', {
    class: 'composer-mic composer-mic-circle',
    type: 'button',
    'aria-label': t('Записать кружок'),
    title: t('Записать кружок'),
  }, icon('circle', { size: 19 }));

  circleButton.addEventListener('click', () => {
    circleMode = !circleMode;
    circleButton.classList.toggle('is-active', circleMode);
    recordButton.classList.toggle('is-circle', circleMode);
    recordButton.setAttribute('aria-label', circleMode ? t('Записать кружок') : t('Записать голосовое сообщение'));
  });

  const meter = el('span', { class: 'composer-meter', 'aria-hidden': 'true' },
    el('span', { class: 'composer-meter-fill' }));
  const timerLabel = recordButton.querySelector('.composer-mic-time');

  const beginRecording = async () => {
    if (recording) return;
    if (!canRecord()) {
      toast.error(t('Браузер не умеет записывать голос'));
      return;
    }
    try {
      recording = await startRecording({
        onLevel: (level) => {
          meter.style.setProperty('--level', String(level));
        },
        onTick: (seconds) => {
          timerLabel.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
        },
      });
    } catch (error) {
      // A refused microphone permission lands here, and so does a browser that
      // cannot reach a device. Both are worth naming rather than failing silently.
      toast.error(error.name === 'NotAllowedError'
        ? t('Нужен доступ к микрофону')
        : t('Не удалось начать запись'));
      recording = null;
    }
  };

  const endRecording = async () => {
    const handle = recording;
    recording = null;
    meter.style.setProperty('--level', '0');
    timerLabel.textContent = '';
    if (!handle) return;

    const clip = await handle.stop();
    if (!clip) return; // released too early to be a message

    try {
      const attachment = await uploadRecording(context.id, clip, circleMode ? 'circle' : 'voice');
      addPending(attachment);
    } catch (error) {
      toast.error(error.message);
    }
  };

  // Hold to record, on the button and on the whole composer, so a thumb resting on
  // the microphone does not have to find the icon exactly.
  recordButton.addEventListener('pointerdown', (event) => {
    event.preventDefault();
    recordButton.classList.add('is-recording');
    beginRecording();
  });
  for (const name of ['pointerup', 'pointercancel', 'pointerleave']) {
    recordButton.addEventListener(name, () => {
      if (!recordButton.classList.contains('is-recording')) return;
      recordButton.classList.remove('is-recording');
      endRecording();
    });
  }
  // A keyboard user gets a click, which starts and stops. Without this the only way
  // to record is a pointer, and hold-to-record has no keyboard gesture of its own.
  recordButton.addEventListener('keydown', (event) => {
    if (event.key === ' ' || event.key === 'Enter') {
      event.preventDefault();
      if (recording) endRecording();
      else {
        recordButton.classList.add('is-recording');
        beginRecording();
      }
    }
  });
  recordButton.addEventListener('keyup', (event) => {
    if ((event.key === ' ' || event.key === 'Enter') && recording) {
      event.preventDefault();
      recordButton.classList.remove('is-recording');
      endRecording();
    }
  });

  composerInput.addEventListener('input', syncSend);

  const node = el('div', { class: 'chat-composer-wrap' },
    tray,
    el('div', { class: 'chat-composer' },
      el('div', { class: 'composer-tools' },
        el('button', {
          class: 'icon-btn',
          type: 'button',
          'aria-label': t('Прикрепить фото'),
          title: t('Фото'),
          onclick: pickPhotos,
        }, icon('image', { size: 19 })),
        el('button', {
          class: 'icon-btn',
          type: 'button',
          'aria-label': t('Прикрепить видео'),
          title: t('Видео'),
          onclick: pickVideos,
        }, icon('video', { size: 19 })),
        circleButton,
        recordButton,
        meter,
      ),
      composerInput,
      sendButton,
    ),
  );

  return {
    node,
    tray,
    pending,
    addPending,
    syncSend,
    isRecording: () => Boolean(recording),
    // Recorded on the context so `unmount` can release the microphone. A recording
    // left running after leaving the thread is a microphone indicator that never
    // goes away, and the reader has no way to stop it.
    stopRecording: () => {
      if (recording) {
        recording.cancel();
        recording = null;
        meter.style.setProperty('--level', '0');
        timerLabel.textContent = '';
        recordButton.classList.remove('is-recording');
      }
    },
    clear() {
      pending.length = 0;
      paintTray();
      syncSend();
    },
  };
}

function bindComposer(context) {
  const { composerInput, sendButton, composer } = context;

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

  const send = async () => {
    const body = composerInput.value.trim();
    const files = [...(composer?.pending || [])];
    if (!body && !files.length) return;

    composerInput.value = '';
    composer?.clear();
    resize();
    realtime.sendTyping(context.id, false);

    // Uploaded files are already on the server, unclaimed. Claiming them is one
    // request; a failure here leaves them unclaimed and the nightly sweep collects
    // them, so nothing is orphaned by a reader losing connection mid-send.
    const mediaIds = files.map((attachment) => attachment.id);
    const optimistic = {
      body,
      attachments: files.map((attachment, index) => ({ ...attachment, id: `local-${index}` })),
      created_at: new Date().toISOString(),
      __pending: true,
    };
    appendMessage(optimistic, { mine: true });

    const offline = !realtime.isConnected();

    if (mediaIds.length) {
      try {
        // The socket's `message:send` carries text only, so an attachment goes over
        // REST. The bubble is already on screen either way, and the socket event
        // that follows replaces it with the stored message.
        await api.post(`/conversations/${context.id}/messages`, { body, media_ids: mediaIds });
      } catch (error) {
        toast.error(error.message);
        const node = document.querySelector('.bubble-pending');
        node?.classList.remove('bubble-pending');
        node?.classList.add('bubble-failed');
      }
    } else {
      realtime.sendMessage(context.id, body);
    }

    if (offline) {
      toast.info(t('Нет соединения — сообщение будет отправлено автоматически.'));
    }
  };

  sendButton.addEventListener('click', send);
  requestAnimationFrame(() => composerInput.focus());
}

async function attachImage(composerInput) {
  // Superseded by the composer's photo button, which uploads into the tray rather
  // than appending a bare URL to the text. Kept for the avatar picker, which
  // genuinely does want a URL back.
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
