/**
 * Vertical video feed: one clip per screen, scrolled with a swipe.
 *
 * The interaction is the familiar one - snap scrolling, tap to pause, the
 * controls on one side rather than below - because that shape genuinely fits
 * watching video one clip at a time. What is on screen is this project's own
 * design: the same warm neutrals, the same thin dividers and type as everywhere
 * else, so the feed reads as part of the site rather than pasted into it.
 *
 * Three rules that the rest of the app already established, kept because they
 * were established the hard way:
 *
 *   - sound is on and there is no autoplay. These conflict, and it has to be
 *     resolved one way: `muted: false` is honoured the moment anything plays, and
 *     a browser that refuses even that will not start the clip at all. `autoplay`
 *     would start every clip in the viewport at once.
 *   - one clip plays at a time, tracked here rather than by a global query, so a
 *     clip that leaves the viewport stops.
 *   - the composer is not here. Publishing happens in a dialog, and a post made
 *     anywhere arrives through `harmony:post-created`.
 *
 * Pagination is keyset, and pages are prefetched one screen ahead: a snap
 * scroller that stops at the end of the list and shows a spinner looks broken,
 * and one that prefetches on every scroll spends the viewer's data on clips they
 * never reach.
 */

import { t } from '../core/i18n.js';

import api from '../core/api.js';
import { clear, el } from '../core/dom.js';
import { compactNumber } from '../core/format.js';
import { icon } from '../core/icons.js';
import store from '../core/store.js';
import toast from '../core/toast.js';
import { avatar, button, copyToClipboard, emptyState, errorState } from '../components/ui.js';
import { openComposer } from '../components/composer.js';
import { confirmDialog, reportDialog } from '../components/report.js';
import { router } from '../core/router.js';

/** How far ahead to fetch, in screens. One is enough to cover the swipe. */
const PREFETCH_AHEAD = 1;

let teardown = [];

/** Every clip on the page, so only one can play at a time. */
let playing = null;

/** Stop whatever is playing. Called on every scroll and every navigation. */
function stopPlayback() {
  if (!playing) return;
  playing.pause();
  playing = null;
}

/**
 * Start a clip, having paused the previous one.
 *
 * Returns quietly rather than throwing when the browser refuses: a phone that
 * will not start playback without a gesture should show a paused first screen,
 * not an error. The reader taps it.
 */
function play(video) {
  stopPlayback();
  const started = video.play();
  if (started && typeof started.catch === 'function') {
    started.catch(() => {
      // Refused. The poster and the play affordance are already on screen, so
      // this is a normal state rather than something to report.
      video.classList.remove('is-playing');
    });
  }
  playing = video;
  video.classList.add('is-playing');
}

export async function render() {
  const currentUser = store.get('currentUser');
  const scroller = el('div', {
    class: 'videos-scroller',
    id: 'videos-scroller',
    tabindex: '0',
    role: 'feed',
    'aria-label': t('Видео'),
  });

  const shell = el('section', { class: 'videos-page' },
    el('header', { class: 'videos-head' },
      el('h1', { class: 'videos-title', text: t('Видео') }),
      el('button', {
        class: 'videos-exit',
        href: '/',
        title: t('Вернуться в ленту'),
        onClick: (event) => {
          event.preventDefault();
          router.navigate('/');
        },
      }, icon('home', { size: 18 }), el('span', { text: t('Лента') })),
    ),
    scroller,
  );

  const state = { cursor: null, loading: false, done: false, started: false };
  const slides = [];

  /** Play whichever clip is nearest the middle, and only that one. */
  const syncPlaybackToScroll = () => {
    if (!slides.length) return;
    const middle = scroller.scrollTop + scroller.clientHeight / 2;
    let best = null;
    let bestDistance = Infinity;
    for (const slide of slides) {
      const centre = slide.offsetTop + slide.offsetHeight / 2;
      const distance = Math.abs(centre - middle);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = slide;
      }
    }
    if (!best) return;

    for (const slide of slides) {
      const video = slide.querySelector('video');
      // `is-current` marks the screen the reader is actually on, which is what
      // the CSS fades in and what the scroll handler uses to decide.
      slide.classList.toggle('is-current', slide === best);
    }

    const video = best.querySelector('video');
    if (!video) return;
    // Tapping pauses, and a paused clip must not be restarted by the scroll
    // handler two pixels later - that is the behaviour that makes a tap feel
    // broken. `userPaused` is per clip and reset when it leaves the viewport.
    if (video.dataset.userPaused === '1') return;
    if (video === playing && !video.paused) return;
    play(video);
  };

  const loadMore = async () => {
    if (state.loading || state.done) return;
    state.loading = true;
    try {
      const params = new URLSearchParams();
      if (state.cursor) params.set('cursor', state.cursor);
      const posts = await api.get(`/videos?${params}`);
      const meta = await Promise.resolve(posts.__meta);

      for (const post of posts) {
        const slide = videoSlide(post, currentUser, { onActivity: syncPlaybackToScroll });
        if (!slide) continue;
        scroller.append(slide);
        slides.push(slide);
      }

      state.cursor = meta?.next_cursor || null;
      state.done = !state.cursor;

      if (!slides.length && state.done) {
        clear(scroller);
        scroller.classList.add('is-empty');
        scroller.append(emptyState({
          iconName: 'sparkle',
          title: t('Пока нет видео'),
          text: t('Снимите первое видео — и оно появится здесь.'),
          action: button(t('Создать публикацию'), {
            variant: 'primary',
            onClick: () => openComposer(),
          }),
        }));
      } else if (state.done && slides.length) {
        scroller.append(el('div', { class: 'videos-end' },
          el('p', { text: t('Это все видео') }),
          button(t('Вернуться в ленту'), {
            onClick: () => router.navigate('/'),
          }),
        ));
      }
    } catch (error) {
      if (!slides.length) {
        clear(scroller);
        scroller.append(errorState({
          text: error.message,
          onRetry: () => {
            state.done = false;
            loadMore();
          },
        }));
      } else {
        toast.error(error.message);
      }
    } finally {
      state.loading = false;
    }
  };

  // Prefetch when the reader is within one screen of the end. Not at the end:
  // waiting until they are already there shows a spinner in the middle of a
  // gesture, which reads as the feed hanging.
  const sentinel = el('div', { class: 'videos-sentinel', 'aria-hidden': 'true' });
  scroller.append(sentinel);
  const observer = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) loadMore();
  }, { root: scroller, rootMargin: `${PREFETCH_AHEAD * 100}% 0px` });
  observer.observe(sentinel);
  teardown.push(() => observer.disconnect());

  let scrollFrame = 0;
  const onScroll = () => {
    // Coalesced to one run per frame: scroll fires far more often than that on
    // a phone, and each run walks every slide.
    if (scrollFrame) return;
    scrollFrame = requestAnimationFrame(() => {
      scrollFrame = 0;
      syncPlaybackToScroll();
    });
  };
  scroller.addEventListener('scroll', onScroll, { passive: true });
  teardown.push(() => scroller.removeEventListener('scroll', onScroll));

  // Keyboard: the scroller is focusable, so arrow keys and space work without a
  // pointer. A feed you can only reach by swiping is unusable with a keyboard.
  const onKeyDown = (event) => {
    if (event.key === 'ArrowDown' || event.key === 'PageDown') {
      event.preventDefault();
      scroller.scrollBy({ top: scroller.clientHeight, behavior: 'smooth' });
    } else if (event.key === 'ArrowUp' || event.key === 'PageUp') {
      event.preventDefault();
      scroller.scrollBy({ top: -scroller.clientHeight, behavior: 'smooth' });
    } else if (event.key === ' ' || event.key === 'k') {
      event.preventDefault();
      toggleCurrent();
    }
  };
  scroller.addEventListener('keydown', onKeyDown);
  teardown.push(() => scroller.removeEventListener('keydown', onKeyDown));

  const toggleCurrent = () => {
    const current = scroller.querySelector('.is-current video');
    if (!current) return;
    if (current.paused) {
      current.dataset.userPaused = '0';
      play(current);
    } else {
      current.dataset.userPaused = '1';
      stopPlayback();
      current.classList.remove('is-playing');
    }
  };

  // A post published from anywhere lands here if it carries video. Not
  // prepended: the reader is mid-feed, and a screen appearing above them would
  // move what they are watching.
  const onPosted = (event) => {
    if (!event.detail?.is_video) return;
    toast.success(t('Видео опубликовано — оно в ленте видео'));
  };
  document.addEventListener('harmony:post-created', onPosted);
  teardown.push(() => document.removeEventListener('harmony:post-created', onPosted));

  // Leaving the page must stop the clip, or sound keeps playing with nothing on
  // screen.
  teardown.push(stopPlayback);

  await loadMore();
  syncPlaybackToScroll();

  return shell;
}

export function unmount() {
  for (const fn of teardown) {
    try {
      fn();
    } catch {
      /* teardown must not throw on the way out */
    }
  }
  teardown = [];
  stopPlayback();
}

/**
 * One screen: the clip, and the reader's controls beside it.
 *
 * Returns null for a post with no playable clip. The endpoint already filters
 * those out, but a clip can fail to load after the fact, and a blank slide with
 * no explanation is worse than one less screen.
 */
function videoSlide(post, currentUser, { onActivity } = {}) {
  const clip = (post.media || []).find((item) => String(item.mime_type || '').startsWith('video/'));
  if (!clip) return null;

  const video = el('video', {
    class: 'videos-clip',
    src: clip.url,
    poster: clip.thumbnail_url || undefined,
    // Sound on, no autoplay. See the note at the top of this file: the two
    // cannot both be honoured, and `muted: false` is the one that matters.
    muted: false,
    loop: '',
    playsinline: '',
    preload: 'metadata',
    // No `controls`. The browser's own control bar is a horizontal strip across
    // the bottom of the video, which sits on top of the caption and the buttons
    // and is drawn in the browser's style rather than the site's.
    controls: null,
    'aria-label': clip.alt_text || post.body || t('Видео'),
  });
  video.dataset.userPaused = '0';

  const playBadge = el('span', { class: 'videos-play-badge', 'aria-hidden': 'true' }, icon('play', { size: 26 }));
  const stage = el('div', { class: 'videos-stage' }, video, playBadge);

  // Tapping the video pauses and resumes. The badge is the only affordance, so
  // it follows the state rather than sitting on top of the picture permanently.
  const toggle = () => {
    const current = stage.querySelector('video');
    if (current.paused) {
      current.dataset.userPaused = '0';
      play(current);
    } else {
      current.dataset.userPaused = '1';
      stopPlayback();
      current.classList.remove('is-playing');
    }
    onActivity?.();
  };
  stage.addEventListener('click', toggle);
  stage.addEventListener('dblclick', (event) => event.preventDefault());

  const actions = el('div', { class: 'videos-actions' },
    videoAction({
      iconName: 'heart',
      label: t('Нравится'),
      count: post.likes_count || 0,
      active: post.viewer_has_liked,
      onClick: async () => {
        if (!currentUser) {
          router.navigate('/login');
          return;
        }
        const optimistic = !post.viewer_has_liked;
        post.viewer_has_liked = optimistic;
        try {
          const result = await api.post(`/posts/${post.id}/reactions`, { type: optimistic ? 'like' : 'unlike' });
          post.likes_count = result.likes_count;
          post.viewer_has_liked = result.liked;
        } catch (error) {
          post.viewer_has_liked = !optimistic;
          toast.error(error.message);
        }
      },
    }),
    videoAction({
      iconName: 'comment',
      label: t('Комментарии'),
      count: post.comments_count || 0,
      onClick: () => router.navigate(`/post/${post.id}`),
    }),
    videoAction({
      iconName: 'bookmark',
      label: t('Сохранить'),
      count: 0,
      onClick: async () => {
        const url = `${location.origin}/post/${post.id}`;
        if (navigator.share) {
          try {
            await navigator.share({ title: t('Гармония'), url });
            return;
          } catch {
            /* dismissed */
          }
        }
        const ok = await copyToClipboard(url);
        if (ok) toast.success(t('Ссылка скопирована'));
      },
    }),
    videoAction({
      iconName: 'more',
      label: t('Ещё'),
      onClick: async (event) => {
        const more = event.currentTarget;
        if (!currentUser) {
          router.navigate('/login');
          return;
        }
        if (post.is_self) {
          const confirmed = await confirmDialog({
            title: t('Удалить видео?'),
            message: t('Оно исчезнет из ленты видео.'),
            confirmLabel: t('Удалить'),
          });
          if (!confirmed) return;
          try {
            await api.delete(`/posts/${post.id}`);
            more.closest('.videos-slide')?.remove();
            toast.success(t('Видео удалено'));
          } catch (error) {
            toast.error(error.message);
          }
          return;
        }
        reportDialog({ targetType: 'post', targetId: post.id });
      },
    }),
  );

  const slide = el('article', {
    class: 'videos-slide',
    dataset: { postId: post.id },
  },
    stage,
    el('div', { class: 'videos-meta' },
      avatar(post.author, { showOnline: true }),
      el('a', {
        class: 'videos-author',
        href: `/u/${encodeURIComponent(post.author?.username || '')}`,
      },
        el('span', { class: 'videos-author-name', text: post.author?.display_name || post.author?.username || '' }),
        post.author?.is_verified ? icon('verified', { size: 15, title: t('Подтверждённый аккаунт') }) : null,
      ),
      post.body ? el('p', { class: 'videos-caption', text: post.body }) : null,
    ),
    actions,
  );

  return slide;
}

function videoAction({ iconName, label, count = 0, active = false, onClick }) {
  const node = el('button', {
    class: 'videos-action',
    type: 'button',
    'aria-label': label,
    'aria-pressed': active ? 'true' : null,
    title: label,
  },
    icon(iconName, { size: 22 }),
    count > 0 ? el('span', { class: 'videos-action-count', text: compactNumber(count) }) : null,
  );
  if (active) node.classList.add('is-active');
  node.addEventListener('click', async (event) => {
    event.stopPropagation();
    if (onClick) await onClick(event);
  });
  return node;
}

