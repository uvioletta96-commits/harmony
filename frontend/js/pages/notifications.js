import { t } from '../core/i18n.js';

﻿/** Notifications inbox. */

import api from '../core/api.js';
import { el, clear } from '../core/dom.js';
import { icon } from '../core/icons.js';
import store, { refreshCounters } from '../core/store.js';
import { router } from '../core/router.js';
import { relativeTime, absoluteTime, colourFor } from '../core/format.js';
import { avatar, button, emptyState, errorState, loadingRow, tabs } from '../components/ui.js';

const KIND_LABELS = {
  like: t('Оценили вашу запись'),
  comment: t('Прокомментировали запись'),
  reply: t('Ответили вам'),
  follow: t('Новый подписчик'),
  message: t('Новое сообщение'),
  moderation: t('Модерация'),
  warning: t('Предупреждение'),
  system: t('Система'),
  reminder: t('Напоминание'),
};

export async function render() {
  if (!store.get('currentUser')) {
    router.navigate('/login', { replace: true });
    return { node: el('div') };
  }

  const list = el('div', { class: 'feed' });
  const shell = el('div', { class: 'layout-center', style: { margin: '0 auto' } },
    el('div', { style: { display: 'flex', justifyContent: 'flex-end', marginBottom: 'var(--space-3)' } },
      button(t('Прочитать все'), {
        variant: 'ghost',
        size: 'sm',
        onClick: async (event) => {
          const { setLoading } = await import('../components/ui.js');
          setLoading(event.currentTarget, true);
          try {
            await api.post('/notifications/read', { all: true });
            await refreshCounters();
            toastSuccess(t('Все уведомления отмечены прочитанными'));
            render2();
          } catch (error) {
            toastError(error.message);
          } finally {
            setLoading(event.currentTarget, false);
          }
        },
      }),
    ),
    list,
  );

  let unreadOnly = false;

  // Held in a closure rather than looked up by id.
  //
  // `render2()` runs while the page is still being built - the router only
  // appends the returned node to `main#main` after `render()` has returned - so
  // `document.getElementById('notif-body')` was null, `load()` returned on its
  // first line, and the spinner it had already drawn stayed on screen for good.
  // That is the "notifications load forever" report, and it is a bug in the
  // order of operations, not in the API: the request was never made.
  const body = el('div', { class: 'notif-body' });

  function render2() {
    clear(list);
    list.append(
      tabs({
        items: [
          { value: 'all', label: t('Все') },
          { value: 'unread', label: t('Непрочитанные') },
        ],
        active: unreadOnly ? 'unread' : 'all',
        onChange: (value) => {
          unreadOnly = value === 'unread';
          load();
        },
      }),
      body,
    );
    load();
  }

  let inFlight = null;

  async function load() {
    // One request at a time. The tab switch, the "mark all read" button and
    // each row's tick all call this, and a slow response from an earlier call
    // would otherwise land on top of a newer one and render the wrong filter.
    if (inFlight) await inFlight.catch(() => {});

    const request = (async () => {
      clear(body);
      body.append(loadingRow());
      try {
        const params = new URLSearchParams();
        if (unreadOnly) params.set('unread', '1');
        const items = await api.get(`/notifications?${params}`);

        clear(body);
        if (!items.length) {
          body.append(
            emptyState({
              iconName: 'bell',
              title: unreadOnly ? t('Непрочитанных нет') : t('Уведомлений пока нет'),
              text: t('Здесь появятся реакции, комментарии и сообщения.'),
            }),
          );
          return;
        }
        for (const item of items) body.append(notificationRow(item, load));
      } catch (error) {
        clear(body);
        body.append(errorState({ text: error.message, onRetry: load }));
      }
    })();

    inFlight = request;
    try {
      await request;
    } finally {
      if (inFlight === request) inFlight = null;
    }
  }

  render2();
  return { node: shell };
}

function notificationRow(notification, reload) {
  const row = el('a', {
    class: `list-row ${notification.is_read ? '' : 'list-unread'}`,
    href: notification.url || '#',
    style: { color: 'inherit', textDecoration: 'none' },
  },
    notification.actor ? avatar(notification.actor, { size: 'sm', link: false }) : el('div', { class: 'avatar avatar-sm', dataset: { color: 'sand' } }, icon('bell', { size: 15 })),
    el('div', { style: { minWidth: '0', flex: '1' } },
      el('div', { style: { fontSize: 'var(--text-sm)', color: 'var(--text)', marginBottom: '2px' } },
        notification.title || KIND_LABELS[notification.kind] || t('Уведомление')),
      notification.body ? el('div', { class: 'text-xs text-muted clamp-2', text: notification.body }) : null,
      el('div', { class: 'text-xs text-muted', title: absoluteTime(notification.created_at), text: relativeTime(notification.created_at) }),
    ),
    !notification.is_read
      ? el('button', {
          class: 'icon-btn',
          type: 'button',
          'aria-label': t('Отметить прочитанным'),
          title: t('Прочитано'),
          onclick: async (event) => {
            event.preventDefault();
            event.stopPropagation();
            try {
              await api.post('/notifications/read', { ids: [notification.id] });
              await refreshCounters();
              reload();
            } catch (error) {
              toastError(error.message);
            }
          },
        }, icon('check', { size: 16 }))
      : null,
  );

  return row;
}

/* Local aliases so the page does not import toast just for two calls. */
let toastModule = null;
async function toastSuccess(message) {
  toastModule ??= (await import('../core/toast.js')).default;
  toastModule.success(message);
}
async function toastError(message) {
  toastModule ??= (await import('../core/toast.js')).default;
  toastModule.error(message);
}
