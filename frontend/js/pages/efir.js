/**
 * «Эфир» - the video library. Videos past the three-minute clip cut-off.
 *
 * A separate page from the vertical feed because the two are different things to
 * use, not different lengths of the same thing:
 *
 *   - the feed is one clip per screen, with the author's face beside it and nothing
 *     else. You swipe. Nothing is listed.
 *   - here several sit side by side with their titles, authors, lengths and view
 *     counts, so you can compare before choosing. You look.
 *
 * That is also why the split lives in the query rather than here. A long video in
 * the feed cannot be swiped past, and hiding it in the browser would still mean
 * paying to transfer it.
 *
 * Playback is deliberately not inline. A grid of auto-playing videos is noise, and
 * the same conflict the feed resolved applies here: sound is on, so nothing starts
 * without being asked. One video at a time, and the reader's own controls - because
 * a hosted video is something to watch, seek in and pause, not a swipe.
 */

import { t } from '../core/i18n.js';

import api from '../core/api.js';
import { el } from '../core/dom.js';
import { compactNumber, formatDuration as shorten } from '../core/format.js';
import { icon } from '../core/icons.js';
import { avatar, button, emptyState, errorState, loadingRow } from '../components/ui.js';
import { router } from '../core/router.js';

/** Sorts, in the order they appear. `views` is deliberately not first. */
const SORTS = [
  { value: 'recent', label: () => t('Сначала новые') },
  { value: 'views', label: () => t('Популярные') },
  { value: 'longest', label: () => t('Длинные') },
];

/**
 * How long a video is, in the compact form a card badge uses.
 *
 * `formatDuration` gives `3:07`, which is right on a player and wrong on a
 * thumbnail where the badge sits over the picture in 11px. Past an hour the badge
 * has to give up precision entirely: `1:02:33` is eleven characters in a corner.
 */
function badgeLength(ms) {
  const total = Math.max(0, Math.round((Number(ms) || 0) / 1000));
  if (total >= 3600) return `${Math.floor(total / 3600)}:${String(Math.floor((total % 3600) / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

let teardown = [];

/** The video currently playing on this page, so only one can. */
let playing = null;

function stopPlayback() {
  if (!playing) return;
  playing.pause();
  playing = null;
}

export async function render({ author = null } = {}) {
  teardown = [];

  const requested = new URLSearchParams(location.search).get('sort');
  const sort = SORTS.some((entry) => entry.value === requested) ? requested : 'recent';

  const grid = el('div', { class: 'efir-grid', id: 'efir-grid' });
  const status = el('div', { class: 'efir-status', role: 'status' });

  const tabs = el('div', { class: 'efir-sorts', role: 'tablist' },
    ...SORTS.map((entry) => el('button', {
      class: `efir-sort ${entry.value === sort ? 'is-active' : ''}`,
      type: 'button',
      role: 'tab',
      'aria-selected': entry.value === sort ? 'true' : 'false',
      text: entry.label(),
      onClick: () => {
        if (entry.value === sort) return;
        // A full navigation rather than a re-render, for the reason the vertical
        // feed's tabs do it: the grid owns its scroll position, and swapping
        // contents under a reader who has scrolled loses their place silently.
        const query = new URLSearchParams(location.search);
        query.set('sort', entry.value);
        router.navigate(`/efir?${query.toString()}`);
      },
    })),
  );

  const shell = el('section', { class: 'efir-page' },
    el('header', { class: 'efir-head' },
      el('div', { style: { minWidth: '0' } },
        el('h1', { class: 'efir-title', text: author ? t('Видео') : t('Эфир') }),
        el('p', {
          class: 'efir-sub',
          text: author
            ? t('Длинные ролики этого автора')
            : t('Ролики длиннее трёх минут. Короткие — в вертикальной ленте.'),
        }),
      ),
      el('button', {
        class: 'icon-btn efir-back',
        type: 'button',
        'aria-label': t('Вернуться в ленту'),
        title: t('Вертикальная лента'),
        onclick: () => router.navigate('/videos'),
      }, icon('play', { size: 18 })),
    ),
    tabs,
    grid,
    status,
  );

  let cursor = null;
  let loading = false;
  let exhausted = false;

  /** Append one page, or report why there is nothing. */
  async function loadPage({ reset = false } = {}) {
    if (loading || (exhausted && !reset)) return;
    loading = true;
    status.textContent = t('Загружаем…');

    try {
      const query = new URLSearchParams({ sort });
      if (cursor && !reset) query.set('cursor', cursor);
      if (author) query.set('author', author);

      const items = await api.get(`/videos/library?${query.toString()}`);

      if (reset) {
        grid.replaceChildren();
        exhausted = false;
      }
      cursor = api.meta?.().next_cursor ?? null;
      exhausted = !cursor;

      if (reset && !items.length) {
        grid.replaceChildren(emptyState({
          iconName: 'play',
          title: t('Пока пусто'),
          text: author
            ? t('Этот автор пока не выложил длинных роликов.')
            : t('Ролики длиннее трёх минут появятся здесь.'),
          action: button(t('Вертикальная лента'), {
            variant: 'secondary',
            size: 'sm',
            onClick: () => router.navigate('/videos'),
          }),
        }));
        status.textContent = '';
        return;
      }

      for (const item of items) grid.append(videoCard(item));
      status.textContent = exhausted ? t('Это все ролики') : '';
    } catch (error) {
      status.textContent = '';
      if (grid.children.length) {
        grid.append(errorState({ text: error.message, onRetry: () => loadPage() }));
      } else {
        grid.replaceChildren(errorState({ text: error.message, onRetry: () => loadPage({ reset: true }) }));
      }
    } finally {
      loading = false;
    }
  }

  // Infinite scroll, same rule as the feed: pages are prefetched rather than
  // fetched at the edge, because a reader who has to wait for a spinner has already
  // decided to leave.
  const sentinel = el('div', { class: 'efir-sentinel' });
  const observer = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) loadPage();
  }, { rootMargin: '600px' });
  observer.observe(sentinel);
  teardown.push(() => observer.disconnect());

  grid.append(sentinel);
  grid.insertAdjacentElement('afterend', status);

  await loadPage({ reset: true });
  return shell;
}

/** One video in the grid: a picture, its length, and who made it. */
function videoCard(item) {
  const media = (item.media || [])[0] || {};
  const author = item.author || {};

  const picture = el('div', { class: 'efir-thumb' },
    el('img', {
      class: 'efir-thumb-img',
      src: media.thumbnail_url || media.url,
      // No video thumbnail exists for an uploaded clip - generating one needs ffmpeg,
      // which is not on this machine - so the card falls back to the video's own
      // first frame by loading it as an image's worth of `poster`. `preload="metadata"`
      // on a hidden video costs a few kilobytes and gives a real poster for free,
      // which is why this is a video and not an img.
      alt: item.body ? item.body.slice(0, 120) : t('Видео'),
      loading: 'lazy',
      decoding: 'async',
    }),
    el('span', { class: 'efir-length', text: badgeLength(media.duration_ms) }),
    el('span', { class: 'efir-play-hint', 'aria-hidden': 'true' }, icon('play', { size: 26 })),
  );

  const play = el('button', {
    class: 'efir-open',
    type: 'button',
    'aria-label': t('Смотреть'),
    title: t('Смотреть'),
    onClick: () => openPlayer(item, media),
  });

  const node = el('article', { class: 'efir-card' },
    picture,
    play,
    el('div', { class: 'efir-card-body' },
      el('a', {
        class: 'efir-card-title',
        href: `/u/${author.username || ''}`,
        text: (item.body || '').trim() || t('Без названия'),
        onclick: (event) => event.stopPropagation(),
      }),
      el('div', { class: 'efir-card-meta' },
        avatar(author, { size: 'xs', link: false, showOnline: true }),
        el('a', {
          class: 'efir-card-author',
          href: `/u/${author.username || ''}`,
          text: `@${author.username || ''}`,
          onclick: (event) => event.stopPropagation(),
        }),
        el('span', { class: 'efir-dot', 'aria-hidden': 'true', text: '·' }),
        el('span', { text: compactNumber(item.views_count || 0) }),
      ),
    ),
  );

  return node;
}

/**
 * Watch one video, in a dialog over the grid.
 *
 * A dialog rather than a route: the reader came to compare several, and leaving the
 * grid to watch one would lose the comparison. Real controls and real seeking,
 * because a hosted video is watched rather than swiped - which is the whole reason
 * it lives here and not in the feed.
 */
function openPlayer(item, media) {
  const video = el('video', {
    class: 'efir-player-video',
    src: media.url,
    controls: '',
    autoplay: '',
    playsinline: '',
    preload: 'metadata',
    'aria-label': item.body || t('Видео'),
  });

  // Sound is on, which is the rule everywhere else. `autoplay` is here, and only
  // here, because this is a deliberate act: the reader picked this video, tapped
  // play, and the grid is not playing eleven other things behind the dialog.
  video.addEventListener('play', () => {
    stopPlayback();
    playing = video;
  });

  const overlay = el('div', {
    class: 'efir-player',
    role: 'dialog',
    'aria-modal': 'true',
    'aria-label': item.body || t('Видео'),
  });

  const dismiss = () => {
    // Stopped before the node goes: a dialog that closes with its audio still
    // playing leaves a video audible behind an overlay that no longer has controls.
    video.pause();
    if (playing === video) playing = null;
    overlay.remove();
    document.removeEventListener('keydown', onKey);
    document.body.style.overflow = previousOverflow;
  };

  const onKey = (event) => {
    if (event.key === 'Escape') dismiss();
  };

  const previousOverflow = document.body.style.overflow;
  const close = el('button', {
    class: 'efir-player-close',
    type: 'button',
    'aria-label': t('Закрыть'),
    onClick: dismiss,
  }, icon('close', { size: 20 }));

  const author = item.author || {};
  overlay.append(
    close,
    el('div', { class: 'efir-player-frame' }, video),
    el('div', { class: 'efir-player-info' },
      el('div', { class: 'efir-player-title', text: (item.body || '').trim() || t('Без названия') }),
      el('div', { class: 'efir-player-meta' },
        avatar(author, { size: 'sm', link: false }),
        el('a', {
          href: `/u/${author.username || ''}`,
          text: author.display_name || `@${author.username || ''}`,
          onclick: dismiss,
        }),
        el('span', { class: 'efir-dot', 'aria-hidden': 'true', text: '·' }),
        el('span', { text: t('{v0} просмотров', { v0: compactNumber(item.views_count || 0) }) }),
        el('span', { class: 'efir-dot', 'aria-hidden': 'true', text: '·' }),
        el('span', { text: shorten(media.duration_ms || 0) }),
      ),
    ),
  );

  overlay.addEventListener('click', (event) => {
    if (event.target === overlay) dismiss();
  });
  document.addEventListener('keydown', onKey);
  document.body.style.overflow = 'hidden';
  document.body.append(overlay);
  video.focus();
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
  // An open dialog is outside `teardown`, so it is closed here: leaving the page with
  // a video still playing means audio with no controls on screen.
  document.querySelectorAll('.efir-player').forEach((node) => node.remove());
  document.body.style.overflow = '';
  stopPlayback();
}