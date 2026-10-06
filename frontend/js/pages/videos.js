/**
 * Vertical feed: one post per screen, filling it with one attachment.
 *
 * Video and photo share the same screen, the same controls and the same gestures.
 * That is deliberate: a separate photo viewer would mean two sets of controls to
 * learn, and the only real difference between watching a clip and looking at a
 * picture is whether sound comes out of it.
 *
 * Interaction is the familiar one - snap scrolling, tap to pause, controls down
 * one side - because that shape genuinely fits watching one thing at a time.
 * What is on screen is this project's own design: the same warm neutrals, thin
 * dividers and type as everywhere else, so the feed reads as part of the site.
 *
 * Four rules that the rest of the app already established, kept because they were
 * established the hard way:
 *
 *   - sound is on and there is no autoplay. These conflict, and it has to be
 *     resolved one way: `muted: false` is honoured the moment anything plays, and
 *     a browser that refuses even that will not start the clip at all. `autoplay`
 *     would start every clip in the viewport at once - measured on this server,
 *     four clips playing with their audio stacked.
 *   - one clip plays at a time, tracked here rather than by a global query, so a
 *     clip that leaves the viewport stops.
 *   - the composer is not here. Publishing happens in a dialog, and a post made
 *     anywhere arrives through `harmony:post-created`.
 *   - pagination is keyset, and pages are prefetched one screen ahead: a snap
 *     scroller that stops at the end and shows a spinner looks broken.
 */

import { t } from '../core/i18n.js';

import api from '../core/api.js';
import { clear, el } from '../core/dom.js';
import { compactNumber, plural } from '../core/format.js';
import { icon } from '../core/icons.js';
import store from '../core/store.js';
import toast from '../core/toast.js';
import { avatar, button, copyToClipboard, emptyState, errorState } from '../components/ui.js';
import { openComposer } from '../components/composer.js';
import { confirmDialog, reportDialog } from '../components/report.js';
import { router } from '../core/router.js';

/** How far ahead to fetch, in screens. One is enough to cover the swipe. */
const PREFETCH_AHEAD = 1;

/** Tabs across the top. `photo` is the same feed for stills. */
const KINDS = [
  { value: 'video', label: () => t('Видео') },
  { value: 'photo', label: () => t('Фото') },
  { value: 'all', label: () => t('Все') },
];

let teardown = [];

/** The clip currently playing, so only one can. */
let playing = null;

/** The slide the reader is on, for the chrome and the keyboard. */
let current = null;

function stopPlayback() {
  if (!playing) return;
  playing.pause();
  playing.classList.remove('is-playing');
  playing = null;
}

/**
 * Start a clip, having paused the previous one.
 *
 * Returns quietly when the browser refuses: a phone that will not start playback
 * without a gesture should show a paused first screen, not an error.
 */
function play(video) {
  stopPlayback();
  const started = video.play();
  if (started && typeof started.catch === 'function') {
    started.catch(() => {
      video.classList.remove('is-playing');
    });
  }
  playing = video;
  video.classList.add('is-playing');
}

export async function render() {
  const currentUser = store.get('currentUser');
  const requestedKind = new URLSearchParams(location.search).get('kind');
  const kind = KINDS.some((k) => k.value === requestedKind) ? requestedKind : 'video';

  const scroller = el('div', {
    class: 'videos-scroller',
    id: 'videos-scroller',
    tabindex: '0',
    role: 'feed',
    'aria-label': t('Видео и фото'),
  });

  // Chrome visibility, remembered across the session rather than per page: a
  // reader who hid it to watch wants it still hidden when they come back.
  let chromeVisible = store.get('videosChrome') !== false;

  const tabs = el('div', { class: 'videos-tabs', role: 'tablist' },
    ...KINDS.map((entry) => el('button', {
      class: 'videos-tab' + (entry.value === kind ? ' is-active' : ''),
      type: 'button',
      role: 'tab',
      'aria-selected': entry.value === kind ? 'true' : 'false',
      text: entry.label(),
      onClick: () => {
        if (entry.value === kind) return;
        // A full navigation rather than a re-render: the feed owns the scroll
        // position, and swapping the contents underneath a reader who is three
        // screens in loses their place without saying so.
        router.navigate(`/videos?kind=${entry.value}`);
      },
    })),
  );

  const chromeToggle = el('button', {
    class: 'videos-icon-button',
    type: 'button',
    title: t('Скрыть оформление'),
    'aria-label': t('Скрыть оформление'),
  }, icon('sliders', { size: 19 }));
  chromeToggle.addEventListener('click', () => setChrome(!chromeVisible));

  const fullscreenButton = el('button', {
    class: 'videos-icon-button',
    type: 'button',
    title: t('Во весь экран'),
    'aria-label': t('Во весь экран'),
  }, icon('expand', { size: 19 }));
  fullscreenButton.addEventListener('click', () => toggleFullscreen());

  const shell = el('section', { class: 'videos-page', dataset: { chrome: chromeVisible ? 'on' : 'off' } },
    el('header', { class: 'videos-head' },
      el('h1', { class: 'videos-title', text: t('Видео и фото') }),
      el('div', { class: 'videos-head-actions' },
        fullscreenButton,
        chromeToggle,
        el('button', {
          class: 'videos-exit',
          type: 'button',
          title: t('Вернуться в ленту'),
          onClick: () => router.navigate('/'),
        }, icon('home', { size: 18 }), el('span', { text: t('Лента') })),
      ),
    ),
    tabs,
    scroller,
  );

  function setChrome(visible) {
    chromeVisible = visible;
    shell.dataset.chrome = visible ? 'on' : 'off';
    chromeToggle.title = visible ? t('Скрыть оформление') : t('Показать оформление');
    chromeToggle.setAttribute('aria-label', chromeToggle.title);
    store.set('videosChrome', visible);
  }

  /** Enter or leave the browser's own fullscreen, on the scroller. */
  async function toggleFullscreen() {
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
      } else if (scroller.requestFullscreen) {
        await scroller.requestFullscreen({ navigationUI: 'hide' });
      } else if (scroller.webkitRequestFullscreen) {
        // Safari on iPhone still only has the prefixed form, and this is the
        // platform most readers are on.
        scroller.webkitRequestFullscreen();
      }
    } catch {
      // Denied - iOS Safari refuses fullscreen on an element inside a page unless
      // the video itself is the target, and a refusal is not worth an error toast.
      toast.warning(t('Браузер не разрешил полноэкранный режим'));
    }
  }

  const onFullscreenChange = () => {
    const on = Boolean(document.fullscreenElement);
    fullscreenButton.replaceChildren(icon(on ? 'collapse' : 'expand', { size: 19 }));
    fullscreenButton.title = on ? t('Выйти из полного экрана') : t('Во весь экран');
    fullscreenButton.setAttribute('aria-label', fullscreenButton.title);
  };
  document.addEventListener('fullscreenchange', onFullscreenChange);
  teardown.push(() => document.removeEventListener('fullscreenchange', onFullscreenChange));

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

    for (const slide of slides) slide.classList.toggle('is-current', slide === best);
    if (best !== current) {
      current = best;
      // A view is counted when a screen is actually reached, not when it is
      // built: prefetching a page would otherwise count six videos the reader
      // never watched, and a view count that inflates on a swipe is worthless.
      const id = best.dataset.postId;
      api.post(`/posts/${id}/views`, {}).catch(() => {
        /* a lost view is not worth reporting; the count is a weak signal anyway */
      });
    }

    const video = best.querySelector('video');
    if (!video) return;
    // `userPaused` is per clip: tapping pauses, and a paused clip must not be
    // restarted by the scroll handler two pixels later. That is the behaviour that
    // makes a tap feel broken.
    if (video.dataset.userPaused === '1') return;
    if (video === playing && !video.paused) return;
    play(video);
  };

  const loadMore = async () => {
    if (state.loading || state.done) return;
    state.loading = true;
    try {
      const params = new URLSearchParams({ kind });
      if (state.cursor) params.set('cursor', state.cursor);
      const posts = await api.get(`/videos?${params}`);
      const meta = await Promise.resolve(posts.__meta);

      for (const post of posts) {
        const slide = feedSlide(post, currentUser, { onActivity: syncPlaybackToScroll });
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
          title: kind === 'photo' ? t('Пока нет фото') : t('Пока нет видео'),
          text: t('Опубликуйте первое — и оно появится здесь.'),
          action: button(t('Создать публикацию'), {
            variant: 'primary',
            onClick: () => openComposer(),
          }),
        }));
      } else if (state.done && slides.length) {
        scroller.append(el('div', { class: 'videos-end' },
          el('p', { text: t('Это все') }),
          button(t('Вернуться в ленту'), { onClick: () => router.navigate('/') }),
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
    // Coalesced to one run per frame: scroll fires far more often than that on a
    // phone, and each run walks every slide.
    if (scrollFrame) return;
    scrollFrame = requestAnimationFrame(() => {
      scrollFrame = 0;
      syncPlaybackToScroll();
    });
  };
  scroller.addEventListener('scroll', onScroll, { passive: true });
  teardown.push(() => scroller.removeEventListener('scroll', onScroll));

  const toggleCurrent = () => {
    const video = current?.querySelector('video');
    if (!video) return;
    if (video.paused) {
      video.dataset.userPaused = '0';
      play(video);
    } else {
      video.dataset.userPaused = '1';
      stopPlayback();
    }
  };

  // Keyboard: the scroller is focusable, so arrows and space work without a
  // pointer. A feed only reachable by swiping is unusable with a keyboard.
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
    } else if (event.key === 'm') {
      event.preventDefault();
      const video = current?.querySelector('video');
      if (video) {
        video.muted = !video.muted;
        toast.info(video.muted ? t('Звук выключен') : t('Звук включён'));
      }
    } else if (event.key === 'f') {
      event.preventDefault();
      toggleFullscreen();
    }
  };
  scroller.addEventListener('keydown', onKeyDown);
  teardown.push(() => scroller.removeEventListener('keydown', onKeyDown));

  // A double tap likes, the way it does in every video feed - and it only fires
  // on a genuine double tap, not on two fast taps that were meant to pause.
  let lastTap = 0;
  scroller.addEventListener('dblclick', (event) => {
    event.preventDefault();
    const slide = event.target.closest('.videos-slide');
    if (slide) burstHeart(slide);
  });
  scroller.addEventListener('click', (event) => {
    const now = Date.now();
    const quick = now - lastTap < 280;
    lastTap = now;
    if (quick) return; // handled by dblclick
    const slide = event.target.closest('.videos-slide');
    if (slide && !event.target.closest('.videos-actions, .videos-meta a')) return;
  });

  // A post published from anywhere lands here. Not prepended: the reader is
  // mid-feed, and a screen appearing above them would move what they are
  // watching.
  const onPosted = (event) => {
    if (event.detail?.is_video || event.detail?.is_photo) {
      toast.success(t('Опубликовано — найдите это во вкладке нужного типа'));
    }
  };
  document.addEventListener('harmony:post-created', onPosted);
  teardown.push(() => document.removeEventListener('harmony:post-created', onPosted));

  // Leaving the page must stop the clip, or sound keeps playing with nothing on
  // screen.
  teardown.push(() => {
    stopPlayback();
    current = null;
  });

  await loadMore();
  syncPlaybackToScroll();

  return shell;
}

/** The heart that rises and fades where a double tap landed. */
function burstHeart(slide) {
  const stage = slide.querySelector('.videos-stage');
  if (!stage) return;
  const heart = el('span', { class: 'videos-burst', 'aria-hidden': 'true' }, icon('heart', { size: 96 }));
  const bounds = stage.getBoundingClientRect();
  heart.style.left = `${bounds.width / 2 - 48}px`;
  heart.style.top = `${bounds.height / 2 - 48}px`;
  stage.append(heart);
  setTimeout(() => heart.remove(), 900);

  const action = slide.querySelector('[data-action="like"]');
  if (action && !action.classList.contains('is-active')) action.click();
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
  current = null;
}

/**
 * One screen: the attachment, and the reader's controls beside it.
 *
 * Returns null for a post with no usable attachment. The endpoint already filters
 * those out, but a file can fail to load after the fact, and a blank slide with no
 * explanation is worse than one less screen.
 */
function feedSlide(post, currentUser, { onActivity } = {}) {
  const attachment = (post.media || [])[0];
  if (!attachment) return null;

  const isVideo = String(attachment.mime_type || '').startsWith('video/');

  let stage;
  if (isVideo) {
    const video = el('video', {
      class: 'videos-clip',
      src: attachment.url,
      poster: attachment.thumbnail_url || undefined,
      // Sound on, no autoplay. See the note at the top of this file: the two
      // cannot both be honoured, and `muted: false` is the one that matters.
      muted: false,
      loop: '',
      playsinline: '',
      preload: 'metadata',
      // No `controls`. The browser's control bar is a horizontal strip across the
      // bottom, drawn in the browser's style, on top of the caption.
      controls: null,
      'aria-label': attachment.alt_text || post.body || t('Видео'),
    });
    video.dataset.userPaused = '0';

    const badge = el('span', { class: 'videos-play-badge', 'aria-hidden': 'true' }, icon('play', { size: 26 }));
    stage = el('div', { class: 'videos-stage' }, video, badge);
    stage.addEventListener('click', () => {
      const currentVideo = stage.querySelector('video');
      if (currentVideo.paused) {
        currentVideo.dataset.userPaused = '0';
        play(currentVideo);
      } else {
        currentVideo.dataset.userPaused = '1';
        stopPlayback();
      }
      onActivity?.();
    });
  } else {
    const image = el('img', {
      class: 'videos-clip videos-still',
      src: attachment.url,
      alt: attachment.alt_text || post.body || t('Фото'),
      // `loading="lazy"`: a slide is built a screen ahead, and decoding three
      // full-resolution photos the reader has not reached is the difference
      // between a smooth scroll and a stutter.
      loading: 'lazy',
      decoding: 'async',
    });
    // A photo screen has no play state, so the badge is never shown - and tapping
    // it should not pretend to pause something that is not playing.
    stage = el('div', { class: 'videos-stage' }, image);
  }

  stage.addEventListener('dblclick', (event) => event.preventDefault());

  const likeButton = feedAction({
    iconName: 'heart',
    action: 'like',
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
      setActionCount(likeButton, optimistic ? post.likes_count + 1 : post.likes_count);
      likeButton.classList.toggle('is-active', optimistic);
      try {
        const result = await api.post(`/posts/${post.id}/reactions`, { type: optimistic ? 'like' : 'unlike' });
        post.likes_count = result.likes_count;
        post.viewer_has_liked = result.liked;
        setActionCount(likeButton, result.likes_count);
        likeButton.classList.toggle('is-active', result.liked);
      } catch (error) {
        post.viewer_has_liked = !optimistic;
        likeButton.classList.toggle('is-active', !optimistic);
        setActionCount(likeButton, post.likes_count);
        toast.error(error.message);
      }
    },
  });

  const commentCount = post.comments_count || 0;
  const commentButton = feedAction({
    iconName: 'comment',
    action: 'comment',
    label: t('Комментарии'),
    count: commentCount,
    onClick: () => router.navigate(`/post/${post.id}`),
  });

  const shareButton = feedAction({
    iconName: 'share',
    action: 'share',
    label: t('Поделиться'),
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
      else toast.warning(t('Не удалось скопировать ссылку'));
    },
  });

  const moreButton = feedAction({
    iconName: 'more',
    action: 'more',
    label: t('Ещё'),
    onClick: async () => {
      if (!currentUser) {
        router.navigate('/login');
        return;
      }
      if (post.is_self) {
        const confirmed = await confirmDialog({
          title: t('Удалить публикацию?'),
          message: t('Она исчезнет из ленты.'),
          confirmLabel: t('Удалить'),
        });
        if (!confirmed) return;
        try {
          await api.delete(`/posts/${post.id}`);
          moreButton.closest('.videos-slide')?.remove();
          toast.success(t('Публикация удалена'));
        } catch (error) {
          toast.error(error.message);
        }
        return;
      }
      reportDialog({ targetType: 'post', targetId: post.id });
    },
  });

  const views = post.views_count || 0;

  return el('article', {
    class: 'videos-slide',
    dataset: { postId: post.id, kind: isVideo ? 'video' : 'photo' },
  },
    stage,
    el('div', { class: 'videos-meta' },
      avatar(post.author, { showOnline: true }),
      el('div', { class: 'videos-meta-text' },
        el('a', {
          class: 'videos-author',
          href: `/u/${encodeURIComponent(post.author?.username || '')}`,
        },
          el('span', { class: 'videos-author-name', text: post.author?.display_name || post.author?.username || '' }),
          post.author?.is_verified ? icon('verified', { size: 15, title: t('Подтверждённый аккаунт') }) : null,
        ),
        post.body ? el('p', { class: 'videos-caption', text: post.body }) : null,
        views > 0
          ? el('p', { class: 'videos-stats' },
              icon('eye', { size: 14 }),
              // The forms are translated; the number goes in afterwards because
              // `plural` selects a form, it does not interpolate one.
              el('span', {
                text: `${compactNumber(views)} ${plural(views, [
                  t('просмотр'),
                  t('просмотра'),
                  t('просмотров'),
                ])}`,
              }),
            )
          : null,
        // Only when the post has more than the one attachment on screen. Otherwise
        // it is a promise the reader cannot keep.
        Number(post.media_count || 0) > 1
          ? el('p', {
              class: 'videos-more-media',
              text: t('ещё {v0}', { v0: String(post.media_count - 1) }),
            })
          : null,
        commentCount > 0
          ? el('p', {
              class: 'videos-teaser',
              text: `${commentCount} ${plural(commentCount, [
                t('комментарий'),
                t('комментария'),
                t('комментариев'),
              ])}`,
            })
          : null,
      ),
    ),
    el('div', { class: 'videos-actions' }, likeButton, commentButton, shareButton, moreButton),
  );
}

function feedAction({ iconName, action, label, count = 0, active = false, onClick }) {
  const node = el('button', {
    class: 'videos-action',
    type: 'button',
    dataset: { action },
    'aria-label': count > 0 ? `${label}: ${compactNumber(count)}` : label,
    'aria-pressed': action === 'like' ? String(active) : null,
    title: label,
  },
    icon(iconName, { size: 22 }),
    el('span', { class: 'videos-action-count' }),
  );
  setActionCount(node, count);
  if (active) node.classList.add('is-active');
  node.addEventListener('click', async (event) => {
    event.stopPropagation();
    if (onClick) await onClick(event);
  });
  return node;
}

function setActionCount(node, count) {
  const label = node.querySelector('.videos-action-count');
  if (!label) return;
  // Empty rather than "0": a column of zeros under every button is noise, and the
  // button's `aria-label` carries the real number for a screen reader.
  label.textContent = count > 0 ? compactNumber(count) : '';
}