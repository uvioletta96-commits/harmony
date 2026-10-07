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
import { compactNumber, pluralIndex as pluralIndexFor } from '../core/format.js';
import { icon } from '../core/icons.js';
import { readFlag, writeValue } from '../core/local.js';
import store from '../core/store.js';
import toast from '../core/toast.js';
import { avatar, button, copyToClipboard, emptyState, errorState } from '../components/ui.js';
import { openComposer } from '../components/composer.js';
import { confirmDialog, reportDialog } from '../components/report.js';
import { router } from '../core/router.js';

/** How far ahead to fetch, in screens. One is enough to cover the swipe. */
const PREFETCH_AHEAD = 1;

/**
 * How long a single tap waits to see whether it is the first half of a double tap.
 *
 * Roughly the platform's own double-click interval. If it is much shorter, a real
 * double tap pauses *and* likes; if it is much longer, a single tap feels laggy -
 * which is worse than the mistake it prevents, so the bias is towards pausing.
 */
const DOUBLE_TAP_MS = 300;

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
    'aria-label': t('Клипы'),
  });

  // Chrome visibility, remembered across the session rather than per page: a
  // reader who hid it to watch wants it still hidden when they come back.
  let chromeVisible = readFlag('videosChrome', true);

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
      el('h1', { class: 'videos-title', text: t('Клипы') }),
      el('div', { class: 'videos-head-actions' },
        // The way to the long form, from inside the short form. Placed beside the
        // fullscreen button rather than in the bottom bar because the bottom bar is
        // hidden in immersive mode, and this is the one navigation the reader needs
        // while watching.
        el('button', {
          class: 'videos-icon-button',
          type: 'button',
          title: t('Длинные ролики — Эфир'),
          'aria-label': t('Длинные ролики — Эфир'),
          onClick: () => router.navigate('/efir'),
        }, icon('film', { size: 18 })),
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
    writeValue('videosChrome', visible);
  }

  /** Enter or leave the browser's own fullscreen, on the scroller. */
  async function toggleFullscreen() {
    const target = scroller;

    // Which element to go fullscreen on.
    //
    // iOS Safari only honours fullscreen on the *video element itself*, and Chrome
    // on Android only on a container that has a size - so the video is the target
    // when there is one, and the scroller otherwise. Requesting the scroller first
    // and falling back is wrong: on iOS it "succeeds" and then shows a black
    // rectangle with no way out, which is the failure this avoids.
    const video = current?.querySelector('video');
    const element = video || target;

    try {
      if (document.fullscreenElement || document.webkitFullscreenElement) {
        await exitFullscreenAnywhere();
        return;
      }
      if (element.requestFullscreen) {
        await element.requestFullscreen();
      } else if (element.webkitRequestFullscreen) {
        // Safari's prefixed form takes no options object and returns undefined
        // rather than a promise.
        element.webkitRequestFullscreen();
      } else {
        toast.warning(t('Браузер не разрешил полноэкранный режим'));
      }
    } catch {
      toast.warning(t('Браузер не разрешил полноэкранный режим'));
    }
  }

  async function exitFullscreenAnywhere() {
    if (document.exitFullscreen) await document.exitFullscreen();
    else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
  }

  const onFullscreenChange = () => {
    const on = Boolean(document.fullscreenElement || document.webkitFullscreenElement);
    fullscreenButton.replaceChildren(icon(on ? 'collapse' : 'expand', { size: 19 }));
    fullscreenButton.title = on ? t('Выйти из полного экрана') : t('Во весь экран');
    fullscreenButton.setAttribute('aria-label', fullscreenButton.title);
    // A screen can enter fullscreen without the button - the reader pressing `f`
    // twice, or the platform's own gesture - and the button has to tell the truth
    // about the state rather than about the last click.
    if (on && !current) syncPlaybackToScroll();
  };
  document.addEventListener('fullscreenchange', onFullscreenChange);
  document.addEventListener('webkitfullscreenchange', onFullscreenChange);
  teardown.push(() => {
    document.removeEventListener('fullscreenchange', onFullscreenChange);
    document.removeEventListener('webkitfullscreenchange', onFullscreenChange);
  });

  const state = { cursor: null, loading: false, done: false, started: false };
  const slides = [];

  /** Play whichever clip is nearest the middle, and only that one. */
  const syncPlaybackToScroll = () => {
    if (!slides.length) return;

    // Viewport coordinates for both sides of the comparison.
    //
    // `offsetTop` looks cheaper and is wrong: it is measured from the nearest
    // *positioned* ancestor, and the scroller is not one - the page is. On the
    // deployed site the first slide reported `offsetTop: 195` while `scrollTop`
    // started at 0, so "nearest slide to the middle of the viewport" was comparing
    // a scroll-relative number against a page-relative one. It flipped three times
    // during a single screen of scrolling, and every flip restarted the clip.
    //
    // `getBoundingClientRect` puts both in the same coordinate system, which is the
    // only property that matters here. One rAF-coalesced pass over the slides is
    // affordable; being wrong is not.
    const viewport = scroller.getBoundingClientRect();
    const middle = viewport.top + viewport.height / 2;
    let best = null;
    let bestDistance = Infinity;
    for (const slide of slides) {
      const rect = slide.getBoundingClientRect();
      const distance = Math.abs(rect.top + rect.height / 2 - middle);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = slide;
      }
    }
    if (!best) return;

    // Once per screen entered, not once per scroll event. `play()` stops whatever
    // was playing first, so any reach of this function that does not change the
    // current screen restarts the clip from zero and the sound cuts and restarts.
    if (best === current) return;

    for (const slide of slides) slide.classList.toggle('is-current', slide === best);
    current = best;

    // A view is counted when a screen is actually reached, not when it is built:
    // prefetching a page would otherwise count six videos the reader never watched,
    // and a view count that inflates on a swipe is worthless.
    const id = best.dataset.postId;
    api.post(`/posts/${id}/views`, {}).catch(() => {
      /* a lost view is not worth reporting; the count is a weak signal anyway */
    });

    const video = best.querySelector('video');
    if (!video) return;
    // `userPaused` is per clip: tapping pauses, and a paused clip must not be
    // restarted when the reader scrolls back to it. Honouring the pause here is
    // what stops the sound from coming back on its own.
    if (video.dataset.userPaused === '1') return;
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
        const slide = feedSlide(post, currentUser);
        if (!slide) continue;
        scroller.append(slide);
        slides.push(slide);
        bindSlideGestures(slide);
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

  // Gestures are bound to each stage as it is built rather than delegated from the
  // scroller. See `bindSlideGestures` for why the delegation was wrong.
  // One pending-tap timer for the whole feed, not one per slide: the gestures are
  // alternatives, and a reader who double taps does not expect both halves of it to
  // resolve independently.
  const tapState = { timer: null };
  const boundListeners = new Map();
  teardown.push(() => {
    // A pending pause that is never cancelled would fire after the page is gone,
    // touching a detached node.
    if (tapState.timer) clearTimeout(tapState.timer);
    tapState.timer = null;
    for (const slide of slides) unbindSlideGestures(slide);
    boundListeners.clear();
  });

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
    } else if (event.key === 'f' || event.key === 'F' || event.key === 'а' || event.key === 'А') {
      event.preventDefault();
      toggleFullscreen();
    }
  };

  // On the document, not on the scroller.
  //
  // The scroller has `tabindex="0"` but nothing focuses it: a reader arrives by
  // tapping, which puts focus on whatever they touched, and the arrow keys work
  // only because the handler was on the element that happened to contain the tap.
  // `f` in particular did nothing at all, because the browser does not focus a
  // scroll container by itself. Listening on the document while this page is
  // mounted is the fix, and the page's own teardown is the natural place to stop.
  //
  // Every key is ignored while the reader is typing, so `f` in the caption of a
  // comment box does not throw the feed into fullscreen.
  const onDocumentKeyDown = (event) => {
    const target = event.target;
    if (target instanceof HTMLElement) {
      if (target.isContentEditable) return;
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    // The scroller's own handler covers these and is bound to the element, so
    // running them twice would page twice.
    if (event.target === scroller || scroller.contains(event.target)) return;
    if (['ArrowDown', 'ArrowUp', 'PageDown', 'PageUp', ' ', 'k', 'm', 'f', 'F', 'а', 'А'].includes(event.key)) {
      onKeyDown(event);
    }
  };
  scroller.addEventListener('keydown', onKeyDown);
  document.addEventListener('keydown', onDocumentKeyDown);
  teardown.push(() => {
    scroller.removeEventListener('keydown', onKeyDown);
    document.removeEventListener('keydown', onDocumentKeyDown);
  });

  // A double tap likes. Bound to the *stage*, not delegated from the scroller: the
  // stage is what a reader aims at, and a scroller-level handler has to work out
  // which slide was hit by walking back up from the target - which is how a double
  // tap on a button both likes the post and presses the button.
  //
  // `tapState` is shared across slides on purpose: a double tap that straddles two
  // screens should still read as one gesture, and a reader's finger does not know
  // where the slide boundary is.
  function bindSlideGestures(slide) {
    const stage = slide.querySelector('.videos-stage');
    if (!stage) return;
    const bound = { stage, onClick: null, onDblClick: null };

    // A single tap pauses and a double tap likes, and the two gestures overlap for
    // the first few hundred milliseconds - so the pause cannot be applied on the
    // first click or every double tap would also pause.
    //
    // It is deferred by one tap interval instead. `dblclick` cancels the pending
    // pause, so a double tap only likes; a single tap pauses a moment later, which
    // is imperceptible and is the same trick every video feed uses. Trying to infer
    // it from `event.detail` alone does not work: the first click of a pair already
    // arrives with `detail === 1`.
    bound.onClick = () => {
      if (tapState.timer) {
        clearTimeout(tapState.timer);
        tapState.timer = null;
      }
      tapState.timer = setTimeout(() => {
        tapState.timer = null;
        togglePlayback(stage);
      }, DOUBLE_TAP_MS);
    };
    bound.onDblClick = (event) => {
      event.preventDefault();
      if (tapState.timer) {
        clearTimeout(tapState.timer);
        tapState.timer = null;
      }
      burstHeart(slide);
    };
    stage.addEventListener('click', bound.onClick);
    stage.addEventListener('dblclick', bound.onDblClick);
    boundListeners.set(stage, bound);
  }

  function unbindSlideGestures(slide) {
    const stage = slide.querySelector('.videos-stage');
    const bound = boundListeners.get(stage);
    if (!bound) return;
    stage.removeEventListener('click', bound.onClick);
    stage.removeEventListener('dblclick', bound.onDblClick);
    boundListeners.delete(stage);
  }

  function togglePlayback(stage) {
    const video = stage.querySelector('video');
    if (!video) return;
    if (video.paused) {
      video.dataset.userPaused = '0';
      play(video);
    } else {
      video.dataset.userPaused = '1';
      stopPlayback();
    }
  }

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

  armAutoFullscreen();

  return shell;
}

/**
 * Go fullscreen on arrival, the way a short-video feed does.
 *
 * What "fullscreen" means here, and why it is two things:
 *
 * 1. Hiding the site's own header, tabs and bottom bar. This needs no permission
 *    and always works, so it happens immediately and unconditionally. It is what
 *    the reader actually sees as "на весь экран" on the phone in the screenshot -
 *    their browser's own bars are already at the top and bottom, and the site's
 *    chrome was the part in the middle eating a fifth of the height.
 *
 * 2. The browser's real fullscreen. This requires a user gesture, so it cannot
 *    happen on load - a synthetic call is refused by every engine. The reader's
 *    first tap, scroll or key press supplies the gesture instead, and that moment
 *    is also when they have shown they want to watch rather than browse. Armed on
 *    a one-shot listener that removes itself the instant it fires, so the second
 *    tap is not swallowed by it and never fights the double-tap gesture.
 *
 * Remembered, not repeated: a reader who left fullscreen once is not put straight
 * back into it on every visit, which is what makes a browser-level mode that the
 * user cannot easily re-enter feel hostile.
 */
function armAutoFullscreen() {
  const IMMERSIVE_KEY = 'videosImmersive';
  const wantsImmersive = readFlag(IMMERSIVE_KEY, true);

  const setImmersive = (on) => {
    document.body.classList.toggle('is-immersive', on);
    writeValue(IMMERSIVE_KEY, on);
  };

  // (1) immediately, and reversible: the explicit exit button turns it off.
  setImmersive(wantsImmersive);

  if (!wantsImmersive) return;

  let armed = true;
  const disarm = () => {
    if (!armed) return;
    armed = false;
    for (const name of ['touchstart', 'pointerdown', 'keydown', 'wheel']) {
      document.removeEventListener(name, fire, { capture: true });
    }
    window.removeEventListener('scroll', fire, { capture: true, passive: true });
  };

  // `scroll` does not count: a reader can arrive with the wheel already turning
  // over a restored scroll position, and that is not a gesture.
  function fire(event) {
    if (event.type === 'keydown' && event.metaKey) return; // cmd+f and friends
    disarm();
    enterBrowserFullscreen();
  }

  for (const name of ['touchstart', 'pointerdown', 'keydown', 'wheel']) {
    document.addEventListener(name, fire, { capture: true, passive: true });
  }
  window.addEventListener('scroll', fire, { capture: true, passive: true });

  teardown.push(disarm);
}

/**
 * Ask for the browser's own fullscreen, swallowing refusal.
 *
 * Silent on failure on purpose: the site's chrome is already hidden by the time
 * this runs, so a refusal costs the reader nothing visible, and a toast saying
 * "не разрешил" on arrival would be noise about something they did not ask for.
 */
function enterBrowserFullscreen() {
  const target = document.querySelector('.is-current video')
    || document.getElementById('videos-scroller');
  if (!target) return;

  try {
    if (document.fullscreenElement || document.webkitFullscreenElement) return;
    if (target.requestFullscreen) {
      // No options object: `navigationUI` is rejected outright by some engines,
      // which throws and costs the whole feature.
      Promise.resolve(target.requestFullscreen()).catch(() => {});
    } else if (target.webkitRequestFullscreen) {
      target.webkitRequestFullscreen();
    }
  } catch {
    /* refused; the site's own chrome is already hidden */
  }
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
  // The immersive class belongs to `body`, which outlives the page. Left behind,
  // every other route would render with its own chrome hidden and no way back.
  document.body.classList.remove('is-immersive');
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
function feedSlide(post, currentUser) {
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
    // No click handler here. `bindSlideGestures` owns taps and double taps on the
    // stage, because they have to be distinguished from each other - a pause here
    // and a like there would mean a double tap both likes and pauses.
    stage = el('div', { class: 'videos-stage' }, video, badge);
  } else {
    // No `loading="lazy"`. That was wrong, and measurably so.
    //
    // A slide is built one screen ahead of the reader, which is the whole reason
    // lazy loading looked right here - there is no point decoding a photo three
    // screens away. But `loading="lazy"` defers to the browser's own viewport
    // estimate, and that estimate does not understand a snap scroller: it measures
    // against the layout viewport, and the slide is inside a nested scroller whose
    // content extends past it. So the first screen - the one the reader is looking
    // at - stayed at `naturalWidth 0`, a blank rectangle with an alt text on it,
    // indefinitely. Verified on the deployed site: the bytes arrived with a 200 and
    // decoded fine as a detached image, but the one on screen never loaded.
    //
    // `decoding="async"` is kept, because deferring the *decode* does not defer
    // deciding to fetch, and it is decode that makes scrolling stutter. The images
    // are already dimension-capped at 2560px by the upload service.
    const image = el('img', {
      class: 'videos-clip videos-still',
      src: attachment.url,
      alt: attachment.alt_text || post.body || t('Фото'),
      decoding: 'async',
      // Width and height from the server's recorded dimensions, so the box is the
      // right shape before the bytes arrive and the caption does not jump.
      width: attachment.width || undefined,
      height: attachment.height || undefined,
    });
    // A photo screen has no play state, so the badge is never shown - and tapping
    // it should not pretend to pause something that is not playing.
    stage = el('div', { class: 'videos-stage' }, image);
  }

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
              // Each plural form is a whole phrase, not a bare noun with the number
              // prepended. Assembling `${n} ${plural(...)}` from separately
              // translated pieces fixes the word order for one language and breaks
              // it for the eleven others: in Arabic the number follows the noun, and
              // nothing in the app can reorder it afterwards.
              el('span', {
                text: [
                  t('{v0} просмотр', { v0: String(views) }),
                  t('{v0} просмотра', { v0: String(views) }),
                  t('{v0} просмотров', { v0: String(views) }),
                ][pluralIndex(views)],
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
              // Whole phrases again, for the same reason as the view count.
              text: [
                t('{v0} комментарий', { v0: String(commentCount) }),
                t('{v0} комментария', { v0: String(commentCount) }),
                t('{v0} комментариев', { v0: String(commentCount) }),
              ][pluralIndex(commentCount)],
            })
          : null,
      ),
    ),
    el('div', { class: 'videos-actions' }, likeButton, commentButton, shareButton, moreButton),
  );
}

/**
 * Which of the three plural forms to use.
 *
 * A thin alias so the two call sites read the same as each other; the rule lives
 * in `core/format.js` next to `plural`, so the two cannot drift apart.
 */
function pluralIndex(count) {
  return pluralIndexFor(count, 3);
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