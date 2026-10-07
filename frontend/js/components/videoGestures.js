import { el } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { t } from '../core/i18n.js';

/**
 * Two gestures that behave the same way in both video surfaces.
 *
 * **Hold to double speed.** Press and keep a finger down; the clip runs at twice the
 * rate and a badge says so. Release and it goes back. Used for the part everybody
 * rereads - the good bit of a walkthrough, the joke in a clip - without a control
 * strip in the way.
 *
 * **Double tap to seek.** One gesture that has to mean both directions, so the
 * screen is split: the left half goes back, the right half goes forward. Anywhere
 * else on the surface would be a guess, and a reader who wanted to go forward and
 * went back would not try again.
 *
 * Both are built on pointer events rather than click, and that is the whole design
 * constraint rather than a modernisation:
 *
 *   - A hold cannot be detected from `click`, which only arrives on release with no
 *     idea how long the finger was down. `pointerdown` arrives immediately.
 *   - A hold and a tap are the same gesture until one of them is longer than the
 *     threshold. So the hold *cancels* the pending tap rather than running after it:
 *     releasing a two-second hold must not also pause the clip.
 *   - `dblclick` fires after the second `click`, so on the clip feed - where a single
 *     tap pauses - the seek would be preceded by a pause and a resume. The
 *     `dblclick` handler therefore cancels the pending pause, exactly as the
 *     double-tap-to-like already does.
 */

/** How long before a press becomes a hold. */
export const HOLD_MS = 250;

/** How far a seek jumps, in seconds. Ten is about as much as fits in one look. */
export const SEEK_SECONDS = 10;

/**
 * Press-and-hold to run at twice the rate.
 *
 * Returns a handle with `cancel()`, because the caller has to be able to stop it on
 * teardown: a rate of 2 left on a clip the reader has navigated away from means the
 * next clip they reach starts at double speed for no stated reason.
 *
 * `own` decides whether a press belongs to this gesture. It is how the two surfaces
 * avoid fighting over the same finger: the clip feed's seek bar owns its own
 * pointer, and the player passes `null` for everything inside the browser's native
 * control bar.
 */
export function holdToSpeed(target, {
  video,
  onStart,
  onEnd,
  own = null,
  threshold = HOLD_MS,
} = {}) {
  let timer = 0;
  let holding = false;

  const release = () => {
    clearTimeout(timer);
    timer = 0;
  };

  const stop = () => {
    release();
    if (!holding) return;
    holding = false;
    video.playbackRate = 1;
    target.classList.remove('is-held');
    onEnd?.();
  };

  const down = (event) => {
    if (event.button !== undefined && event.button !== 0) return;
    // A second finger is not a hold: it is somebody pinching to zoom or scrubbing,
    // and dropping the rate mid-gesture is worse than not offering the gesture.
    if (event.isPrimary === false) return;
    if (own && !own(event)) return;

    release();
    timer = setTimeout(() => {
      timer = 0;
      holding = true;
      video.playbackRate = 2;
      target.classList.add('is-held');
      onStart?.();
    }, threshold);
  };

  const lift = () => stop();

  target.addEventListener('pointerdown', down);
  target.addEventListener('pointerup', lift);
  target.addEventListener('pointercancel', lift);
  target.addEventListener('pointerleave', lift);

  return {
    /** True while the gesture is running, so a caller can suppress its own taps. */
    get holding() {
      return holding;
    },
    cancel: stop,
  };
}

/**
 * Double tap to seek, left for back and right for forward.
 *
 * The indicator is the feedback. A seek with no visible response reads as a dropped
 * frame, and a reader will tap again, and again, until the clip is somewhere they
 * did not ask for.
 */
export function doubleTapToSeek(target, {
  video,
  seconds = SEEK_SECONDS,
  onSeek,
  own = null,
} = {}) {
  let timer = 0;
  let indicator = null;

  const hide = () => {
    clearTimeout(timer);
    timer = 0;
    indicator?.remove();
    indicator = null;
  };

  const flash = (text, forward) => {
    hide();
    indicator = el('span', { class: `seek-flash ${forward ? 'is-forward' : 'is-back'}` },
      icon(forward ? 'chevronRight' : 'chevronLeft', { size: 30 }),
      el('span', { class: 'seek-flash-text', text }),
    );
    target.append(indicator);
    // Held a little longer than the seek takes to be noticed, then gone. The
    // animation itself carries most of it; the removal is the backstop for a
    // browser with reduced motion on.
    timer = setTimeout(hide, 620);
  };

  const dbl = (event) => {
    event.preventDefault();
    hide();
    if (own && !own(event)) return;

    const bounds = target.getBoundingClientRect();
    // The click's own coordinates rather than the target's: during a double tap the
    // pointer has usually moved a few pixels, and a reader who meant to tap the
    // right side and landed one pixel over the midpoint should not go backwards.
    const x = event.clientX || bounds.left + bounds.width / 2;
    const forward = x > bounds.left + bounds.width / 2;

    const limit = Number(video.duration) || 0;
    const from = video.currentTime || 0;
    const to = forward
      ? Math.min(limit || Infinity, from + seconds)
      : Math.max(0, from - seconds);
    video.currentTime = to;

    // The whole phrase goes through `t`, with the count as an argument.
    //
    // Joining a translated word to a number by hand is the mistake the plural rules
    // exist to prevent: the reader's language decides the word order and whether the
    // unit is even spelled the same way, and a concatenation cannot know that.
    if (forward) {
      flash(t('Вперёд на {v0}', { v0: t('{v0} с', { v0: seconds }) }), true);
    } else {
      flash(t('Назад на {v0}', { v0: t('{v0} с', { v0: seconds }) }), false);
    }
    onSeek?.({ forward, from, to });
  };

  target.addEventListener('dblclick', dbl);

  return {
    cancel: hide,
  };
}
