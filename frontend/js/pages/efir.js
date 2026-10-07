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
 * Playback is deliberately not automatic in the grid. Sound is on everywhere else in
 * this project, so a page that starts eleven videos by itself would be unusable. One
 * video at a time, and the reader's own controls - because a hosted video is
 * something to watch, seek in and pause, not a swipe.
 *
 * The layout is mobile-first and the desktop is the adaptation, not the other way
 * round. On a phone the grid is one column and the shelves are the primary
 * navigation, because a shelf that can be flicked past is cheaper than a menu on a
 * screen 360px wide.
 */

import { t } from '../core/i18n.js';

import api from '../core/api.js';
import { el } from '../core/dom.js';
import { compactNumber, formatDuration, relativeTime } from '../core/format.js';
import { icon } from '../core/icons.js';
import { readFlag, writeValue } from '../core/local.js';
import { avatar, button, emptyState, errorState, loadingRow } from '../components/ui.js';
import { posterElement } from '../components/posters.js';
import {
  isSaved,
  laterIds,
  rememberPosition,
  resumeAt,
  resumeEntries,
  toggleSaved,
  watchedPercent,
} from '../components/watchState.js';
import { router } from '../core/router.js';
import toast from '../core/toast.js';

/** Sorts, in the order they appear. `views` is deliberately not first. */
const SORTS = [
  { value: 'recent', label: () => t('Сначала новые') },
  { value: 'views', label: () => t('Популярные') },
  { value: 'longest', label: () => t('Длинные') },
];

/** Shown at the top of the library, in this order. */
const SHELVES = [
  { value: 'all', label: () => t('Все') },
  { value: 'continue', label: () => t('Продолжить') },
  { value: 'later', label: () => t('Смотреть позже') },
];

/** Playback speeds a hosted video offers. */
const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 2];

let teardown = [];

/** The video currently playing, so only one can. */
let playing = null;
/** The shell, so a shelf can refresh itself without a re-render. */
let currentView = null;

function stopPlayback() {
  if (!playing) return;
  playing.pause();
  playing = null;
}

export async function render({ author = null } = {}) {
  teardown = [];

  const params = new URLSearchParams(location.search);
  const requestedSort = params.get('sort');
  const sort = SORTS.some((entry) => entry.value === requestedSort) ? requestedSort : 'recent';
  const requestedShelf = params.get('shelf');
  const shelf = SHELVES.some((entry) => entry.value === requestedShelf) ? requestedShelf : 'all';

  /** Everything fetched so far, so a shelf can be rebuilt from one list. */
  const library = new Map();

  const grid = el('div', { class: 'efir-grid', id: 'efir-grid' });
  const status = el('div', { class: 'efir-status', role: 'status', 'aria-live': 'polite' });

  // The sentinel lives *outside* the grid. The earlier version appended it to the
  // grid, and the first load called `grid.replaceChildren()` - which detached the
  // node the IntersectionObserver was watching. The observer then measured a node
  // that was in no document, so the second page was never requested and the library
  // silently stopped at twenty-four videos.
  const sentinel = el('div', { class: 'efir-sentinel', 'aria-hidden': 'true' });

  const shelfRow = el('div', { class: 'efir-shelves', role: 'tablist' });
  const sortRow = el('div', { class: 'efir-sorts', role: 'tablist' });

  const search = searchField(() => rebuild());
  const shelves = el('div', { class: 'efir-shelf-host' });

  const head = el('header', { class: 'efir-head' },
    el('div', { class: 'efir-head-text' },
      el('h1', { class: 'efir-title', text: author ? t('Видео') : t('Эфир') }),
      el('p', {
        class: 'efir-sub',
        text: author
          ? t('Длинные ролики этого автора')
          : t('Ролики длиннее трёх минут. Короткие — в вертикальной ленте.'),
      }),
    ),
    el('div', { class: 'efir-head-actions' },
      search,
      el('button', {
        class: 'icon-btn',
        type: 'button',
        'aria-label': t('Вертикальная лента'),
        title: t('Короткие ролики'),
        onclick: () => router.navigate('/videos'),
      }, icon('play', { size: 18 })),
    ),
  );

  const shell = el('section', { class: 'efir-page' },
    head,
    shelfRow,
    sortRow,
    shelves,
    grid,
    sentinel,
    status,
  );

  currentView = { shelf, rebuild };

  renderShelves(shelfRow, shelf);
  renderSorts(sortRow, sort);

  let cursor = null;
  let loading = false;
  let exhausted = false;
  let query = '';

  /** Append one page of the library, or say why there is nothing. */
  async function loadPage({ reset = false } = {}) {
    if (loading || (exhausted && !reset)) return;
    loading = true;
    if (reset) status.textContent = t('Загружаем…');

    try {
      const request = new URLSearchParams({ sort });
      if (cursor && !reset) request.set('cursor', cursor);
      if (author) request.set('author', author);

      const items = await api.get(`/videos/library?${request.toString()}`);

      // The cursor comes off the payload's `__meta`, not off `api`. `api.meta` has
      // never existed: `unwrap` attaches the envelope's meta to the *data* as a
      // non-enumerable property so a paged list can stay a plain array. The old call
      // was `api.meta?.()`, which is undefined on every response, so the cursor was
      // always null and `exhausted` was true after the first page.
      cursor = items?.__meta?.next_cursor || null;
      exhausted = !cursor;

      if (reset) {
        library.clear();
        grid.replaceChildren();
      }
      for (const item of items || []) library.set(item.id, item);

      render();
    } catch (error) {
      status.textContent = '';
      if (library.size) {
        grid.append(errorState({ text: error.message, onRetry: () => loadPage() }));
      } else {
        grid.replaceChildren(errorState({ text: error.message, onRetry: () => loadPage({ reset: true }) }));
      }
    } finally {
      loading = false;
    }
  }

  /** Everything below, drawn from whatever has been fetched. */
  function render() {
    status.textContent = exhausted && library.size ? t('Это все ролики') : '';
    renderShelves(shelfRow, shelf);

    const matched = filterByQuery([...library.values()], query);
    const chosen = shelf === 'all' ? matched : shelfItems(shelf, matched);

    grid.replaceChildren();

    if (!chosen.length) {
      grid.append(emptyFor(shelf, query));
      shelves.replaceChildren();
      return;
    }

    for (const item of chosen) grid.append(videoCard(item));
    shelves.replaceChildren(...shelfBlocks(matched));
  }

  /** Redraw from what is already in memory. No request. */
  function rebuild() {
    query = search.value.trim();
    render();
  }

  const observer = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) loadPage();
  }, { rootMargin: '700px' });
  observer.observe(sentinel);
  teardown.push(() => observer.disconnect());

  grid.append(loadingRow(t('Загружаем ролики…')));
  await loadPage({ reset: true });
  return shell;
}

/**
 * Case-insensitive match over the title, the author's name and their handle.
 *
 * `needle` is a parameter, not a closure read. The first version read a `query`
 * belonging to `render`'s scope, which a module-level function cannot see - so the
 * very first render threw `query is not defined` and the library showed an error
 * card with no videos at all.
 */
function filterByQuery(items, needle) {
  if (!needle) return items;
  const wanted = needle.toLocaleLowerCase();
  return items.filter((item) => {
    const author = item.author || {};
    return (
      (item.body || '').toLocaleLowerCase().includes(wanted)
      || (author.display_name || '').toLocaleLowerCase().includes(wanted)
      || (author.username || '').toLocaleLowerCase().includes(wanted)
    );
  });
}

/** The two shelves that are derived from what this browser has done. */
function shelfItems(which, fetched) {
  if (which === 'continue') {
    const byId = new Map(fetched.map((item) => [item.id, item]));
    return resumeEntries()
      .map((entry) => byId.get(entry.id))
      .filter(Boolean);
  }
  if (which === 'later') {
    const wanted = laterIds();
    const byId = new Map(fetched.map((item) => [item.id, item]));
    return wanted.map((id) => byId.get(id)).filter(Boolean);
  }
  return fetched;
}

/**
 * Horizontal rows above the grid, on a phone.
 *
 * A shelf rather than a list because flicking sideways to compare beats scrolling a
 * long page of cards, and because the two shelves are not part of the library's
 * order - they are what *this* browser has done, and mixing them into the sorted
 * grid would silently reorder the catalogue by personal history.
 */
function shelfBlocks(fetched) {
  const byId = new Map(fetched.map((item) => [item.id, item]));
  const blocks = [];

  const resuming = resumeEntries()
    .map((entry) => byId.get(entry.id))
    .filter(Boolean)
    .slice(0, 12);
  if (resuming.length) {
    blocks.push(shelfBlock(t('Продолжить'), resuming, 'continue'));
  }

  const saved = laterIds()
    .map((id) => byId.get(id))
    .filter(Boolean)
    .slice(0, 12);
  if (saved.length) {
    blocks.push(shelfBlock(t('Смотреть позже'), saved, 'later'));
  }

  return blocks;
}

function shelfBlock(title, items, kind) {
  const row = el('div', { class: 'efir-shelf-row' },
    el('div', { class: 'efir-shelf-head' },
      el('h2', { class: 'efir-shelf-title', text: title }),
      el('button', {
        class: 'efir-shelf-more',
        type: 'button',
        onClick: () => navigate({ shelf: kind }),
        text: t('Все'),
      }),
    ),
    el('div', { class: 'efir-shelf-track' },
      ...items.map((item) => el('div', { class: 'efir-shelf-cell' }, videoCard(item, { compact: true }))),
    ),
  );
  return row;
}

function renderShelves(host, active) {
  host.replaceChildren(
    ...SHELVES.map((entry) => el('button', {
      class: `efir-shelf-tab ${entry.value === active ? 'is-active' : ''}`,
      type: 'button',
      role: 'tab',
      'aria-selected': entry.value === active ? 'true' : 'false',
      text: entry.label(),
      onClick: () => navigate({ shelf: entry.value }),
    })),
  );
}

function renderSorts(host, active) {
  host.replaceChildren(
    ...SORTS.map((entry) => el('button', {
      class: `efir-sort ${entry.value === active ? 'is-active' : ''}`,
      type: 'button',
      role: 'tab',
      'aria-selected': entry.value === active ? 'true' : 'false',
      text: entry.label(),
      // A full navigation rather than a re-render: the grid owns its scroll
      // position, and swapping the contents under a reader who has scrolled loses
      // their place without saying so.
      onClick: () => navigate({ sort: entry.value }),
    })),
  );
}

function navigate(patch) {
  const params = new URLSearchParams(location.search);
  for (const [key, value] of Object.entries(patch)) {
    if (value) params.set(key, value);
    else params.delete(key);
  }
  router.navigate(`/efir${params.toString() ? `?${params}` : ''}`);
}

function emptyFor(shelf, needle) {
  if (needle) {
    return emptyState({
      iconName: 'search',
      title: t('Ничего не найдено'),
      text: t('Попробуйте другое слово.'),
    });
  }
  if (shelf === 'continue') {
    return emptyState({
      iconName: 'clock',
      title: t('Пока нечего продолжать'),
      text: t('Ролик, который вы не досмотрели, появится здесь.'),
    });
  }
  if (shelf === 'later') {
    return emptyState({
      iconName: 'bookmark',
      title: t('Список пуст'),
      text: t('Нажмите на закладку под роликом, чтобы вернуться к нему позже.'),
    });
  }
  return emptyState({
    iconName: 'film',
    title: t('Пока пусто'),
    text: t('Ролики длиннее трёх минут появятся здесь.'),
    action: button(t('Вертикальная лента'), {
      variant: 'secondary',
      size: 'sm',
      onClick: () => router.navigate('/videos'),
    }),
  });
}

/** A search box that filters what is already loaded. */
function searchField(onChange) {
  const input = el('input', {
    class: 'input efir-search-input',
    type: 'search',
    placeholder: t('Поиск по «Эфиру»'),
    'aria-label': t('Поиск роликов'),
    autocomplete: 'off',
  });
  let timer = 0;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    // Debounced because `render()` rebuilds every card, and rebuilding a screenful
    // on each keystroke is visible on a phone.
    timer = setTimeout(onChange, 180);
  });
  return input;
}

/** One video: a poster, its length, and who made it. */
function videoCard(item, { compact = false } = {}) {
  const media = (item.media || [])[0] || {};
  const author = item.author || {};
  const saved = isSaved(item.id);
  const watched = watchedPercent(item.id);

  const poster = posterElement(media.url || '', media.storage_key || item.id);

  const title = (item.body || '').trim() || t('Без названия');

  const saveButton = el('button', {
    class: `efir-save ${saved ? 'is-saved' : ''}`,
    type: 'button',
    'aria-label': saved ? t('Убрать из «Смотреть позже»') : t('Смотреть позже'),
    title: saved ? t('Убрать из «Смотреть позже»') : t('Смотреть позже'),
    onclick: (event) => {
      event.stopPropagation();
      const nowSaved = toggleSaved(item.id);
      saveButton.classList.toggle('is-saved', nowSaved);
      saveButton.replaceChildren(icon('bookmark', { size: 16 }));
      saveButton.setAttribute('aria-label', nowSaved ? t('Убрать из «Смотреть позже»') : t('Смотреть позже'));
      saveButton.title = saveButton.getAttribute('aria-label');
      // The shelf above the grid is derived from the same list, so it has to be told
      // rather than left showing a video that is no longer saved.
      currentView?.rebuild();
    },
  }, icon('bookmark', { size: 16 }));

  const open = el('button', {
    class: 'efir-open',
    type: 'button',
    'aria-label': t('Смотреть: {v0}', { v0: title }),
    onClick: () => openPlayer(item, media),
  });

  return el('article', { class: `efir-card ${compact ? 'efir-card-compact' : ''}` },
    el('div', { class: 'efir-thumb' },
      poster,
      el('span', { class: 'efir-length', text: formatDuration(media.duration_ms) }),
      // A progress line rather than a number: "осталось 12 минут" is what a
      // part-watched video is for, and the bar is readable without reading.
      watched ? el('span', { class: 'efir-progress', style: { '--watched': `${watched}%` } }) : null,
      el('span', { class: 'efir-play-hint', 'aria-hidden': 'true' }, icon('play', { size: 26 })),
      saveButton,
      open,
    ),
    el('div', { class: 'efir-card-body' },
      el('div', { class: 'efir-card-title', text: title }),
      el('div', { class: 'efir-card-meta' },
        avatar(author, { size: 'xs', link: false, showOnline: true }),
        el('span', { class: 'efir-card-author', text: author.display_name || `@${author.username || ''}` }),
        el('span', { class: 'efir-dot', 'aria-hidden': 'true', text: '·' }),
        el('span', { text: t('{v0} просмотров', { v0: compactNumber(item.views_count || 0) }) }),
      ),
    ),
  );
}

/**
 * Watch one video.
 *
 * A dialog on a wide screen and a full-screen sheet on a phone - the same element
 * either way, because a phone dialog with a video in it has borders and a title bar
 * the reader does not want, and a desktop that goes full-bleed for one video looks
 * broken. CSS decides which; this builds one thing.
 *
 * The reader came to compare several, so it is not a route: navigating would lose
 * the comparison, and the back gesture would have to unwind the whole page first.
 */
function openPlayer(item, media) {
  const resumeFrom = resumeAt(item.id);
  const video = el('video', {
    class: 'efir-player-video',
    src: media.url,
    controls: '',
    autoplay: '',
    playsinline: '',
    preload: 'metadata',
    'aria-label': (item.body || '').trim() || t('Видео'),
  });

  // One video at a time, and it stays up while the shelf behind scrolls on a phone.
  video.addEventListener('play', () => {
    stopPlayback();
    playing = video;
    document.body.classList.add('has-player');
  });

  // The position, written on a timer rather than on every `timeupdate`, which fires
  // about four times a second and would mean four localStorage writes a second.
  let lastSaved = 0;
  let saveTimer = 0;
  video.addEventListener('timeupdate', () => {
    if (Date.now() - lastSaved < 4000) return;
    lastSaved = Date.now();
    rememberPosition(item.id, video.currentTime * 1000, video.duration * 1000);
  });

  const author = item.author || {};
  const title = (item.body || '').trim() || t('Без названия');

  const overlay = el('div', {
    class: 'efir-player',
    role: 'dialog',
    'aria-modal': 'true',
    'aria-label': title,
  });

  const speedButton = el('button', {
    class: 'efir-speed',
    type: 'button',
    'aria-label': t('Скорость'),
    title: t('Скорость воспроизведения'),
  }, el('span', { text: '1×' }));

  const openSpeed = () => {
    const menu = el('div', { class: 'efir-speed-menu', role: 'menu' },
      ...SPEEDS.map((speed) => el('button', {
        class: `efir-speed-item ${video.playbackRate === speed ? 'is-active' : ''}`,
        type: 'button',
        role: 'menuitem',
        text: `${speed}×`,
        onClick: () => {
          video.playbackRate = speed;
          speedButton.querySelector('span').textContent = `${speed}×`;
          menu.remove();
        },
      })),
    );
    overlay.querySelector('.efir-speed-menu')?.remove();
    speedButton.parentElement.append(menu);
    const away = (event) => {
      if (!menu.contains(event.target)) {
        menu.remove();
        document.removeEventListener('pointerdown', away, true);
      }
    };
    setTimeout(() => document.addEventListener('pointerdown', away, true), 0);
  };
  speedButton.addEventListener('click', openSpeed);

  // `m` for sound, `f` for fullscreen, arrows for seeking - only where a keyboard
  // exists. On a phone they are inert, and listening for them there would swallow
  // nothing useful, so the guard is what keeps the shortcuts out of the way.
  const onKey = (event) => {
    if (event.target instanceof HTMLElement) {
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(event.target.tagName)) return;
      if (event.target.isContentEditable) return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey) return;

    if (event.key === 'Escape') {
      dismiss();
    } else if (event.key === ' ' || event.key === 'k') {
      event.preventDefault();
      if (video.paused) video.play().catch(() => {});
      else video.pause();
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      video.currentTime = Math.min(video.duration || 0, video.currentTime + 10);
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      video.currentTime = Math.max(0, video.currentTime - 10);
    } else if (event.key === 'm' || event.key === 'M' || event.key === 'ь' || event.key === 'Ь') {
      event.preventDefault();
      video.muted = !video.muted;
      toast.info(video.muted ? t('Звук выключен') : t('Звук включён'));
    } else if (event.key === 'f' || event.key === 'F' || event.key === 'а' || event.key === 'А') {
      event.preventDefault();
      toggleFullscreen(video);
    }
  };

  const dismiss = () => {
    // Saved before the node goes, and only if there is somewhere to save it to: a
    // video that never loaded has no duration and would store NaN.
    if (video.duration && Number.isFinite(video.duration)) {
      rememberPosition(item.id, video.currentTime * 1000, video.duration * 1000);
    }
    clearInterval(saveTimer);
    video.pause();
    if (playing === video) playing = null;
    document.body.classList.remove('has-player');
    overlay.remove();
    document.removeEventListener('keydown', onKey);
    document.body.style.overflow = previousOverflow;
    // The card's progress bar and the shelves are derived from the same saved
    // positions, so they have to be redrawn - otherwise a video the reader has just
    // finished still shows a progress line on the card behind.
    currentView?.rebuild();
  };

  const previousOverflow = document.body.style.overflow;
  const close = el('button', {
    class: 'efir-player-close',
    type: 'button',
    'aria-label': t('Закрыть'),
    onClick: dismiss,
  }, icon('close', { size: 20 }));

  const frame = el('div', { class: 'efir-player-frame' }, video, el('div', { class: 'efir-player-speed' }, speedButton));
  const info = el('div', { class: 'efir-player-info' },
    el('div', { class: 'efir-player-title', text: title }),
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
      el('span', { text: relativeTime(item.created_at) }),
    ),
  );

  overlay.append(close, frame, info);
  overlay.addEventListener('click', (event) => {
    if (event.target === overlay) dismiss();
  });
  document.addEventListener('keydown', onKey);
  document.body.style.overflow = 'hidden';
  document.body.append(overlay);
  video.focus();

  // Resume once the browser knows how long the video is. Setting `currentTime`
  // before that is ignored, which is why this is in `loadedmetadata` rather than
  // straight after `append`.
  video.addEventListener('loadedmetadata', () => {
    if (resumeFrom > 0) {
      video.currentTime = Math.min(resumeFrom / 1000, Math.max(0, (video.duration || 0) - 1));
      toast.info(t('Продолжаем с {v0}', { v0: formatDuration(resumeFrom) }));
    }
  }, { once: true });

  // A position is written on a timer while playing, so a reader who closes the tab
  // rather than the dialog still keeps their place. The `timeupdate` handler covers
  // ordinary watching; this covers the case where the video is playing and no
  // `timeupdate` has fired recently - a paused-then-backgrounded tab, mostly.
  saveTimer = setInterval(() => {
    if (!video.paused && video.duration && Number.isFinite(video.duration)) {
      rememberPosition(item.id, video.currentTime * 1000, video.duration * 1000);
    }
  }, 10000);
}

function toggleFullscreen(video) {
  try {
    const on = document.fullscreenElement || document.webkitFullscreenElement;
    if (on) {
      if (document.exitFullscreen) document.exitFullscreen();
      else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
      return;
    }
    if (video.requestFullscreen) {
      // No options object: `navigationUI` is rejected outright by some engines, and
      // the throw costs the whole feature.
      Promise.resolve(video.requestFullscreen()).catch(() => {});
    } else if (video.webkitRequestFullscreen) {
      video.webkitRequestFullscreen();
    }
  } catch {
    /* refused; the player is already full-bleed, so nothing is lost */
  }
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
  // An open player is outside `teardown`, so it is closed here: leaving the page
  // with a video still playing means audio with no controls on screen.
  document.querySelectorAll('.efir-player').forEach((node) => node.remove());
  document.body.classList.remove('has-player');
  document.body.style.overflow = '';
  currentView = null;
  stopPlayback();
}

/** Whether this browser remembers a preference for the clip feed's chrome. */
export function chromeHidden() {
  return readFlag('videosChrome', true) === false;
}

/** Remembered, and this time actually saved. See `core/local.js`. */
export function rememberChrome(visible) {
  writeValue('videosChrome', visible);
}