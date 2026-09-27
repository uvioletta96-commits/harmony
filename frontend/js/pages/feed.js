/**
 * Feed page: an infinitely scrolling list of posts.
 *
 * There is no composer here. Writing happens in a dialog opened from the "+"
 * in the top bar or the button on a phone, and a post published from anywhere
 * arrives here through the `harmony:post-created` event.
 *
 * Pagination is keyset-based. The cursor for the next page comes from the
 * response metadata, and a sentinel element plus IntersectionObserver triggers
 * the next load — no scroll-position maths, no "load more" button, and no
 * duplicate or skipped items when new posts arrive mid-scroll.
 */

import { t } from '../core/i18n.js';

import api from '../core/api.js';
import { el, clear } from '../core/dom.js';
import { icon } from '../core/icons.js';
import store from '../core/store.js';
import toast from '../core/toast.js';
import {
  postCard,
  emptyState,
  errorState,
  skeletonRows,
  loadingRow,
  button,
  copyToClipboard,
} from '../components/ui.js';
import { confirmDialog, reportDialog } from '../components/report.js';
import { editPostDialog } from '../components/editPost.js';
import { router } from '../core/router.js';

let dispose = [];

/** Heading per feed mode. Kept beside the tabs so a new mode cannot ship
 *  with a tab and no title. */
const TITLES = {
  for_you: t('Для вас'),
  shuffled: t('Случайно'),
  latest: t('Новое'),
  following: t('Подписки'),
};

export async function render() {
  const currentUser = store.get('currentUser');
  // The server's default is the personalised shuffle; asking for nothing is
  // the same as asking for it, so the tab and the request cannot disagree.
  const mode = new URLSearchParams(location.search).get('mode') || 'for_you';

  const list = el('div', { class: 'feed', id: 'feed-list' });
  const shell = el('section', { class: 'layout-center', style: { margin: '0 auto' } });

  const titleRow = el('div', { class: 'feed-head' },
    el('h1', { class: 'feed-title', text: TITLES[mode] || TITLES.for_you }),
    el('div', { class: 'segmented', role: 'tablist' },
      segmentButton('Для вас', 'for_you', mode),
      segmentButton('Случайно', 'shuffled', mode),
      segmentButton('Новое', 'latest', mode),
      segmentButton('Подписки', 'following', mode),
    ),
  );

  const feedPanel = el('div', { class: 'feed' }, titleRow, list);
  shell.append(feedPanel);

  // A post published from the compose dialog, wherever it was opened from.
  //
  // Removed again in `unmount` below: a page that has been navigated away from
  // must not keep prepending posts into a list nobody can see, and a second
  // visit would otherwise stack another listener on top of the first.
  const onPosted = (event) => prepend(list, event.detail);
  document.addEventListener('harmony:post-created', onPosted);

  list.append(skeletonRows(3));

  const state = { cursor: null, loading: false, done: false, mode };

  /** Drop every placeholder: both the initial skeletons and the spinner row. */
  const clearPlaceholders = () => {
    for (const node of list.querySelectorAll('.post-skeleton, .loading-row')) node.remove();
  };

  const loadMore = async () => {
    if (state.loading || state.done) return;
    state.loading = true;
    if (list.querySelectorAll('.post').length) {
      list.append(loadingRow('Загружаем новые публикации…'));
    }
    try {
      const params = new URLSearchParams();
      // Always sent, including for the default. A cursor only means anything
      // together with the ordering that produced it, so the two travel as one.
      params.set('mode', state.mode);
      if (state.cursor) params.set('cursor', state.cursor);

      const posts = await api.get(`/feed?${params}`);
      const meta = await Promise.resolve(posts.__meta);
      clearPlaceholders();

      for (const post of posts) list.append(renderPost(post, list, currentUser));
      state.cursor = meta?.next_cursor || null;
      state.done = !meta?.has_more;

      if (state.done) {
        list.append(el('div', { class: 'empty', style: { padding: 'var(--space-6)' } },
          el('p', { class: 'empty-text', text: 'Вы посмотрели все публикации.' }),
        ));
      }
    } catch (error) {
      clearPlaceholders();
      if (!list.querySelector('.post')) {
        clear(list);
        list.append(errorState({ text: error.message, onRetry: () => { clear(list); loadMore(); } }));
      } else {
        list.append(el('div', { class: 'empty' },
          el('p', { class: 'empty-text', text: 'Не удалось загрузить следующую страницу.' }),
          button('Повторить', { variant: 'secondary', size: 'sm', onClick: () => { list.querySelector('.empty')?.remove(); loadMore(); } }),
        ));
      }
    } finally {
      state.loading = false;
    }
  };

  const observer = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) loadMore();
    },
    { rootMargin: '600px 0px' },
  );
  const sentinel = el('div', { class: 'sentinel' });
  list.append(sentinel);
  observer.observe(sentinel);

  const unsubscribe = store.subscribe('currentUser', () => router.resolve());

  await loadMore();

  return {
    node: shell,
    unmount() {
      observer.disconnect();
      unsubscribe();
      document.removeEventListener('harmony:post-created', onPosted);
      dispose.forEach((fn) => fn());
      dispose = [];
    },
  };
}

function segmentButton(label, value, active) {
  return el('a', {
    href: `/?mode=${value}`,
    role: 'tab',
    'aria-selected': value === active ? 'true' : 'false',
    class: value === active ? 'is-active' : '',
    style: {
      padding: '5px 12px',
      borderRadius: 'var(--radius-sm)',
      fontSize: 'var(--text-sm)',
      fontWeight: '500',
      textDecoration: 'none',
      color: value === active ? 'var(--text)' : 'var(--text-muted)',
    },
    text: label,
  });
}


function prepend(list, post) {
  const first = list.querySelector('.post');
  list.querySelectorAll('.empty').forEach((node) => node.remove());
  const node = renderPost(post, list, store.get('currentUser'));
  if (first) {
    list.insertBefore(node, first);
    // Fade the new item in so it is obvious something was added.
    node.animate(
      [{ opacity: 0, transform: 'translateY(-8px)' }, { opacity: 1, transform: 'translateY(0)' }],
      { duration: 320, easing: 'cubic-bezier(0.22,1,0.36,1)' },
    );
  } else {
    list.prepend(node);
  }
  fetchMediaNow(node);
}

/**
 * Make a just-published post's media actually load.
 *
 * `renderPost` builds the whole subtree while it is still detached, and its
 * images carry `loading="lazy"`. A lazy image decides whether to fetch when it
 * first becomes renderable, so one that is created detached and then inserted
 * has its fetch cancelled by the insertion - and it is not retried. The element
 * sits there with a real `src`, a real size and `currentSrc` still empty, which
 * is a grey box where the photo should be.
 *
 * It only shows up on this path. The same image in a feed that arrived from the
 * server was built inside the document and loads normally, and the identical URL
 * in a fresh `Image()` loads fine - so it is the detached construction, not the
 * file, the server or the URL.
 *
 * Promoting to `eager` after insertion restarts the pending fetch, and it is the
 * right behaviour anyway: this is the reader's own post, at the top of their
 * feed, and its picture is the point of the thing they just did.
 */
function fetchMediaNow(node) {
  for (const media of node.querySelectorAll('img[loading="lazy"]')) {
    media.loading = 'eager';
  }
}

/**
 * One post in the feed.
 *
 * The handlers live in a named object rather than inline in the call, because
 * editing re-renders this exact card with the same handlers - and a handler
 * set that exists only as arguments to the first call cannot be passed to the
 * second one.
 */
export function renderPost(post, list, currentUser) {
  const handlers = {
    currentUser,

    onLike: async (event, target) => {
      const action = event.currentTarget;
      if (!currentUser) {
        router.navigate('/login');
        return;
      }
      const optimistic = !target.viewer_has_liked;
      // Optimistic update: flip immediately, reconcile with the response.
      action.classList.toggle('is-active', optimistic);
      action.classList.toggle('is-bouncing', optimistic);
      action.querySelector('.count').textContent = String(Math.max(0, target.likes_count + (optimistic ? 1 : -1)) || '');
      try {
        const result = await api.post(`/posts/${target.id}/reactions`, { type: optimistic ? 'like' : 'unlike' });
        target.viewer_has_liked = result.liked;
        action.setAttribute('aria-pressed', String(result.liked));
        action.querySelector('.count').textContent = result.likes_count > 0 ? String(result.likes_count) : '';
      } catch (error) {
        action.classList.toggle('is-active', !optimistic);
        action.querySelector('.count').textContent = String(target.likes_count || '');
        toast.error(error.message);
      }
    },

    onComment: () => router.navigate(`/post/${target.id}`),

    onEdit: async (target) => {
      const updated = await editPostDialog(target);
      if (!updated) return;
      // The card is replaced, not reloaded: the reader is mid-scroll and a
      // reload would lose their place in the feed.
      const node = list?.querySelector(`[data-post-id="${target.id}"]`);
      const replacement = postCard(updated, handlers);
      if (node) node.replaceWith(replacement);
      else list?.prepend(replacement);
    },

    onShare: async (target) => {
      const url = `${location.origin}/post/${target.id}`;
      if (navigator.share) {
        try {
          await navigator.share({ title: t('Гармония'), url });
          return;
        } catch {
          /* the reader dismissed the share sheet */
        }
      }
      const ok = await copyToClipboard(url);
      if (ok) toast.success(t('Ссылка скопирована'));
      else toast.warning(t('Не удалось скопировать ссылку'));
    },

    onReport: (target) => reportDialog({ targetType: 'post', targetId: target.id }),

    onDelete: async (target) => {
      const confirmed = await confirmDialog({
        title: t('Удалить публикацию?'),
        message: t('Она исчезнет из ленты. Комментарии сохранятся, но останутся без контекста.'),
        confirmLabel: t('Удалить'),
      });
      if (!confirmed) return;
      try {
        await api.delete(`/posts/${target.id}`);
        list?.querySelector(`[data-post-id="${target.id}"]`)?.remove();
        toast.success(t('Публикация удалена'));
      } catch (error) {
        toast.error(error.message);
      }
    },
  };

  return postCard(post, handlers);
}
