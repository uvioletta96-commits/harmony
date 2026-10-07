/**
 * A poster frame for a video, drawn in the browser.
 *
 * There is no thumbnail on the server, and there cannot be one: making one means
 * decoding the file, which means ffmpeg, which is not on this machine. Every other
 * option is worse -
 *
 *   - putting the video's own URL in an `<img>`. That renders a broken-image icon on
 *     every card, which is what the library did before this existed.
 *   - leaving `poster` empty, so the frame is black until the reader presses play.
 *     A grid of black rectangles is not browsable.
 *
 * So the frame is taken from the video itself. The browser already has a demuxer and
 * a decoder; the work here is loading, seeking to a moment worth showing, and
 * drawing one `<canvas>`.
 *
 * Two details that decide whether the result looks right:
 *
 *   - Seek to a fraction, not to zero. The first frame of a video is very often a
 *     black fade-in, a title card, or a logo on a black field - technically an
 *     accurate poster and useless as one. A tenth of the way in is nearly always a
 *     picture.
 *   - Seek *and wait for the frame*, in that order. Setting `currentTime` before
 *     `loadedmetadata` is ignored, and setting it and immediately drawing gets the
 *     previous frame - which for the first draw is nothing at all.
 */

import { el } from '../core/dom.js';

import { cachePoster, cachedPoster } from './watchState.js';

/** Width of a drawn poster. Enough for a card, small enough to cache. */
const WIDTH = 480;

/** How far in to look for a frame worth showing. */
const FRACTION = 0.12;

/** Give up after this long. A card is not worth blocking the shelf for. */
const TIMEOUT_MS = 9000;

/**
 * In flight, so twenty cards on one screen do not each load the same video.
 *
 * Keyed by url. Without it, scrolling a shelf of cards from the same author opens
 * the same file twenty times.
 */
const inFlight = new Map();

/**
 * A poster for `url`, or null if one cannot be drawn.
 *
 * Never rejects: a card without a picture is fine, a card that throws while its
 * siblings render is not.
 */
export function posterFor(url, storageKey = '') {
  const cached = storageKey ? cachedPoster(storageKey) : null;
  if (cached) return Promise.resolve(cached);

  const pending = inFlight.get(url);
  if (pending) return pending;

  const job = drawPoster(url)
    .then((dataUrl) => {
      if (dataUrl && storageKey) cachePoster(storageKey, dataUrl);
      return dataUrl;
    })
    .catch(() => null)
    .finally(() => inFlight.delete(url));

  inFlight.set(url, job);
  return job;
}

function drawPoster(url) {
  return new Promise((resolve) => {
    const video = document.createElement('video');
    let settled = false;

    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      // Everything this video holds is released here. A `<video>` left with a
      // source keeps its decoder and its buffered data alive, and a shelf of thirty
      // cards is thirty of them.
      video.removeAttribute('src');
      video.load?.();
      resolve(value);
    };

    const timer = setTimeout(() => finish(null), TIMEOUT_MS);

    video.muted = true;
    video.playsInline = true;
    video.preload = 'metadata';
    // Cross-origin bytes would taint the canvas and `toDataURL` would throw. The
    // uploads are same-origin, so this is belt and braces rather than a fix.
    video.crossOrigin = 'anonymous';

    video.addEventListener('loadedmetadata', () => {
      const duration = Number(video.duration);
      if (!Number.isFinite(duration) || duration <= 0) {
        finish(null);
        return;
      }
      const target = Math.min(duration * FRACTION, Math.max(0, duration - 0.25));
      if (target <= 0) {
        finish(null);
        return;
      }
      video.addEventListener('seeked', () => finish(capture(video)), { once: true });
      video.addEventListener('error', () => finish(null), { once: true });
      video.currentTime = target;
    }, { once: true });

    video.addEventListener('error', () => finish(null), { once: true });
    video.src = url;
  });
}

/** One frame, drawn to a data URL, or null if the canvas says no. */
function capture(video) {
  const sourceWidth = video.videoWidth;
  const sourceHeight = video.videoHeight;
  if (!sourceWidth || !sourceHeight) return null;

  const height = Math.round((WIDTH * sourceHeight) / sourceWidth);
  const canvas = document.createElement('canvas');
  canvas.width = WIDTH;
  canvas.height = height;

  const context = canvas.getContext('2d');
  if (!context) return null;
  try {
    context.drawImage(video, 0, 0, WIDTH, height);
    return canvas.toDataURL('image/jpeg', 0.72);
  } catch {
    // A tainted canvas throws here. Same-origin uploads never get there; this is
    // what happens if that assumption is ever wrong.
    return null;
  }
}

/**
 * A picture element for a card, filled in as soon as a frame can be drawn.
 *
 * A `<video>` rather than an `<img>`: the browser holds the frame for us and gives
 * it to the canvas without a second copy of the bytes in the DOM, and `poster` is
 * exactly the slot this belongs in.
 *
 * The element exists immediately with a neutral backdrop, so the grid has its shape
 * before any of the pictures exist - a page that reflows once per card is worse
 * than one that fills in.
 */
export function posterElement(url, storageKey) {
  const frame = el('video', {
    class: 'efir-thumb-video',
    preload: 'metadata',
    muted: '',
    playsinline: '',
    'aria-hidden': 'true',
    tabindex: '-1',
  });

  // Never plays. The element is a decoder, not a player, and one that could start
  // would be one more thing making noise.
  frame.addEventListener('play', () => frame.pause(), { capture: true });

  posterFor(url, storageKey).then((dataUrl) => {
    if (!dataUrl) {
      frame.classList.add('is-unavailable');
      return;
    }
    frame.classList.add('is-ready');
    frame.style.backgroundImage = `url("${dataUrl}")`;
  });

  return frame;
}