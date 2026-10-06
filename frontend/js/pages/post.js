/**
 * Post detail page with a threaded comment section.
 */

import { t } from '../core/i18n.js';

import api from '../core/api.js';
import { el, clear } from '../core/dom.js';
import { icon } from '../core/icons.js';
import store from '../core/store.js';
import toast from '../core/toast.js';
import { router } from '../core/router.js';
import { postCard, commentItem, button, setLoading, emptyState, errorState, loadingRow, alert, copyToClipboard } from '../components/ui.js';
import { reportDialog, confirmDialog } from '../components/report.js';
import { editPostDialog } from '../components/editPost.js';

export async function render({ id }) {
  const currentUser = store.get('currentUser');
  const shell = el('div', { class: 'layout-center', style: { margin: '0 auto' } });
  const container = el('div', { class: 'post-detail' });
  shell.append(loadingRow(t('Открываем публикацию…')));

  let post;
  try {
    post = (await api.get(`/posts/${id}`)).post;
  } catch (error) {
    clear(shell);
    shell.append(
      errorState({
        title: error.status === 404 ? t('Публикация не найдена') : t('Не удалось открыть публикацию'),
        text: error.status === 404
          ? t('Возможно, она удалена или закрыта настройками приватности.')
          : error.message,
        onRetry: () => router.resolve(),
      }),
    );
    return { node: shell };
  }

  document.title = t(`Публикация · Гармония`);

  clear(shell);
  let card;
  card = postCard(post, {
    currentUser,
    onLike: (event) => toggleLike(post, event.currentTarget),
    onShare: () => share(post),
    onReport: () => reportDialog({ targetType: 'post', targetId: post.id, targetPreview: post.body?.slice(0, 200) }),
    onEdit: async (target) => {
      const updated = await editPostDialog(target);
      if (!updated) return;
      // Swap the body in place. A reload would discard the scroll position and
      // every comment already loaded underneath.
      const bodyNode = card.querySelector('.post-text');
      if (bodyNode) {
        bodyNode.textContent = updated.body;
        bodyNode.hidden = !updated.body;
      }
      post.body = updated.body;
      post.edited_at = updated.edited_at;
    },
    onDelete: async () => {
      const confirmed = await confirmDialog({
        title: t('Удалить публикацию?'),
        message: t('Она исчезнет из ленты. Комментарии сохранятся, но останутся без контекста.'),
        confirmLabel: t('Удалить'),
      });
      if (!confirmed) return;
      try {
        await api.delete(`/posts/${post.id}`);
        toast.success(t('Публикация удалена'));
        router.navigate('/');
      } catch (error) {
        toast.error(error.message);
      }
    },
  });
  // A single post on its own page should not repeat the permalink.
  card.querySelector('.post-actions')?.lastElementChild?.remove();

  // The composer and the list live in separate containers on purpose.
  // `loadComments` clears the list to render a fresh page of results, and the
  // composer used to sit in that same node: it was inserted before the first
  // load and destroyed by it, so a signed-in reader got "no comments yet" and
  // no box to type in. Anything that rebuilds the list must leave the composer
  // alone, so it cannot be a child of what gets cleared.
  const commentsHost = el('div', { id: 'comments' });
  const composerSlot = el('div', { id: 'comment-composer' });
  const commentList = el('div', { id: 'comment-list' });
  commentsHost.append(composerSlot, commentList);
  container.append(card, commentsHost);
  shell.append(container);

  /* --- Comments -------------------------------------------------------- */

  const state = { cursor: null, done: false, loading: false, sort: 'top' };

  const loadComments = async ({ append = false } = {}) => {
    if (state.loading || (state.done && append)) return;
    state.loading = true;
    if (!append) commentList.append(loadingRow(t('Загружаем комментарии…')));

    try {
      const params = new URLSearchParams({ sort: state.sort });
      if (append && state.cursor) params.set('cursor', state.cursor);

      const data = await api.get(`/posts/${post.id}/comments?${params}`);
      const comments = Array.isArray(data) ? data : data;
      commentList.querySelector('.loading-row')?.remove();
      if (!append) clear(commentList);

      if (!comments.length && !append) {
        commentList.append(
          emptyState({
            iconName: 'comment',
            title: t('Пока нет комментариев'),
            text: currentUser ? t('Будьте первым, кто откликнется.') : t('Войдите, чтобы оставить комментарий.'),
            action: currentUser ? null : button(t('Войти'), { variant: 'secondary', size: 'sm', onClick: () => router.navigate('/login') }),
          }),
        );
      } else {
        if (!append) commentList.append(commentsHeader(state));
        for (const comment of comments) {
          commentList.append(renderComment(comment));
        }
        state.cursor = comments.__meta?.next_cursor || null;
        state.done = !comments.__meta?.has_more;

        if (state.done && comments.length) {
          commentList.append(
            el('p', { class: 'text-center text-muted text-sm', style: { padding: 'var(--space-4)' }, text: t('Это все комментарии') }),
          );
        } else if (!state.done) {
          const more = button(t('Показать ещё'), {
            variant: 'ghost',
            size: 'sm',
            onClick: (event) => {
              setLoading(event.currentTarget, true);
              loadComments({ append: true });
            },
          });
          commentList.append(el('div', { class: 'text-center', style: { padding: 'var(--space-3)' } }, more));
        }
      }
    } catch (error) {
      commentList.querySelector('.loading-row')?.remove();
      commentList.append(alert({ variant: 'danger', text: error.message }));
    } finally {
      state.loading = false;
    }
  };

  const renderComment = (comment) =>
    commentItem(comment, {
      currentUser,
      onReply: (target) => openReplyBox(target),
      onDelete: async (target) => {
        const confirmed = await confirmDialog({
          title: t('Удалить комментарий?'),
          message: t('Комментарий будет скрыт, а обсуждение сохранится.'),
          confirmLabel: t('Удалить'),
        });
        if (!confirmed) return;
        try {
          await api.delete(`/comments/${target.id}`);
          document.getElementById(`comment-${target.id}`)?.remove();
          toast.success(t('Комментарий удалён'));
        } catch (error) {
          toast.error(error.message);
        }
      },
    });

  const commentsHeader = (currentSort) =>
    el('div', { class: 'comments-head' },
      el('h2', { class: 'comments-title', text: t('Комментарии') }),
      el('div', { class: 'segmented' },
        sortButton(t('Популярные'), 'top', currentSort.sort),
        sortButton(t('Новые'), 'newest', currentSort.sort),
      ),
    );

  const sortButton = (label, value, active) =>
    el('button', {
      type: 'button',
      'aria-selected': value === active ? 'true' : 'false',
      style: { padding: '5px 12px', borderRadius: 'var(--radius-sm)', fontSize: 'var(--text-sm)', fontWeight: '500' },
      onclick: () => {
        state.sort = value;
        state.cursor = null;
        state.done = false;
        clear(commentList);
        loadComments();
      },
    }, label);

  /* --- Composer -------------------------------------------------------- */

  if (currentUser) {
    composerSlot.append(buildCommentBox(null));
  }

  function buildCommentBox(parent) {
    const area = el('textarea', {
      class: 'composer-input',
      rows: '2',
      placeholder: parent ? t('Ответить {v0}…', { v0: parent.author?.display_name || parent.author?.username || '' }) : t('Оставить комментарий…'),
      maxlength: '2000',
      'aria-label': parent ? t('Текст ответа') : t('Текст комментария'),
    });
    const send = button(parent ? t('Ответить') : t('Отправить'), { variant: 'primary', size: 'sm', disabled: true, onClick: submit });
    const errorSlot = el('div');

    area.addEventListener('input', () => {
      send.disabled = area.value.trim().length === 0;
      area.style.height = 'auto';
      area.style.height = `${Math.min(area.scrollHeight, 200)}px`;
    });

    async function submit() {
      const body = area.value.trim();
      if (!body) return;
      setLoading(send, true);
      clear(errorSlot);
      try {
        const result = await api.post(`/posts/${post.id}/comments`, {
          body,
          parent_id: parent?.id || null,
        });
        area.value = '';
        area.style.height = '';
        send.disabled = true;
        if (parent) {
          // A reply box exists for one reply; the main box is not a one-shot
          // form. Removing it after the first comment is what left a reader
          // able to say exactly one thing per visit.
          box.remove();
          const host = document.getElementById(`comment-${parent.id}`);
          host?.append(renderComment(result.comment));
        } else {
          commentList.querySelector('.empty')?.remove();
          // Into the list, at the top - not next to it. Inserting relative to
          // `commentList` put the new comment between the composer and the
          // list: it rendered, but outside the container the list reloads and
          // the count reads from, so nothing downstream saw it and it vanished
          // on the next sort change. Confirmed on the deployed server: the
          // comment was in the DOM and not in `#comment-list`.
          commentList.prepend(renderComment(result.comment));
          // Refocused so a reader writing several comments in a row does not
          // have to reach for the field again.
          area.focus();
        }
        toast.success(parent ? t('Отправлено') : t('Комментарий добавлен'));
      } catch (error) {
        errorSlot.append(alert({ variant: 'danger', text: error.message }));
      } finally {
        setLoading(send, false);
      }
    }

    area.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        submit();
      }
    });

    const box = el('div', { class: 'composer', style: { borderTop: '1px solid var(--border-soft)', borderBottom: 'none' } },
      el('div', { class: 'composer-row' }, area),
      errorSlot,
      el('div', { class: 'composer-toolbar' },
        el('span', { class: 'hint', text: t('Ctrl + Enter — отправить') }),
        parent ? button(t('Отмена'), { variant: 'ghost', size: 'sm', onClick: () => box.remove() }) : null,
        send,
      ),
    );
    return box;
  }

  function openReplyBox(parent) {
    document.querySelector('.reply-box')?.remove();
    const box = buildCommentBox(parent);
    box.classList.add('reply-box');
    document.getElementById(`comment-${parent.id}`)?.after(box);
    box.querySelector('textarea')?.focus();
  }

  await loadComments();

  return { node: shell };
}

async function toggleLike(post, action) {
  if (!store.get('currentUser')) {
    router.navigate('/login');
    return;
  }
  const optimistic = !post.viewer_has_liked;
  action.classList.toggle('is-active', optimistic);
  action.classList.toggle('is-bouncing', optimistic);
  try {
    const result = await api.post(`/posts/${post.id}/reactions`, { type: optimistic ? 'like' : 'unlike' });
    post.viewer_has_liked = result.liked;
    action.setAttribute('aria-pressed', String(result.liked));
    action.querySelector('.count').textContent = result.likes_count > 0 ? String(result.likes_count) : '';
  } catch (error) {
    action.classList.toggle('is-active', !optimistic);
    toast.error(error.message);
  }
}

async function share(post) {
  const url = `${location.origin}/post/${post.id}`;
  if (navigator.share) {
    try {
      await navigator.share({ title: t('Гармония'), url });
      return;
    } catch { /* dismissed */ }
  }
  const ok = await copyToClipboard(url);
  if (ok) toast.success(t('Ссылка скопирована'));
  else toast.warning(t('Не удалось скопировать ссылку'));
}
