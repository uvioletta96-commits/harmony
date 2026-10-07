/**
 * What the reader has watched, started and saved - in this browser.
 *
 * Three separate things, kept apart on purpose:
 *
 *   - **Continue watching.** A position, not a flag. Updated while playing, and a
 *     video is only offered again while it is genuinely part-finished: past a minute
 *     in, and with more than two minutes left to watch. Under that it is a video
 *     somebody just watched, and a shelf of those is noise.
 *   - **Watch later.** An explicit save. Never expires and never moves on its own,
 *     because the reader asked for it by tapping.
 *   - **Poster cache.** The first frame of each video, drawn in the browser, so the
 *     library has pictures without a thumbnail existing on the server.
 *
 * All three are per-browser rather than per-account. A `localStorage` value cannot
 * be scoped to a server-side user, and mixing two people's viewing history into one
 * bucket on a shared machine is worse than either of them having to tap once. The
 * site's own draft handling makes the same trade for the same reason.
 */

import { readList, readValue, writeValue } from '../core/local.js';

const RESUME_KEY = 'efir:resume';
const LATER_KEY = 'efir:later';
const POSTER_KEY = 'posters';

/** Below this, a video counts as finished rather than part-watched. */
const RESUME_FLOOR_MS = 60_000;

/** And it is only offered again with at least this much still to watch. */
const RESUME_MIN_REMAINING_MS = 120_000;

/** Most remembered positions, so the shelf is a shelf and not an archive. */
const RESUME_LIMIT = 24;

/** Most saved videos. Beyond this the shelf stops being usable. */
const LATER_LIMIT = 200;

// ---------------------------------------------------------------------------
// Continue watching
// ---------------------------------------------------------------------------

export function resumeEntries() {
  const raw = readValue(RESUME_KEY, []);
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((entry) => entry && typeof entry.id === 'string' && Number(entry.at) > 0)
    .sort((a, b) => Number(b.at) - Number(a.at));
}

/** Record a position. Called on a timer, so it must stay cheap. */
export function rememberPosition(id, positionMs, durationMs) {
  const duration = Number(durationMs) || 0;
  const position = Number(positionMs) || 0;
  if (!id || duration <= 0) return;

  const entries = resumeEntries().filter((entry) => entry.id !== id);
  const remaining = duration - position;

  // Started, and finished, or finished - either way it is not part-watched.
  if (position < RESUME_FLOOR_MS || remaining < RESUME_MIN_REMAINING_MS) {
    writeValue(RESUME_KEY, entries);
    return;
  }

  entries.unshift({ id, position: Math.round(position), duration, at: Date.now() });
  writeValue(RESUME_KEY, entries.slice(0, RESUME_LIMIT));
}

/** Forget one, for a video the reader has finished or deleted. */
export function forgetPosition(id) {
  writeValue(RESUME_KEY, resumeEntries().filter((entry) => entry.id !== id));
}

/** Where this video should resume from, in milliseconds. Zero means from the start. */
export function resumeAt(id) {
  const entry = resumeEntries().find((row) => row.id === id);
  if (!entry) return 0;
  const position = Number(entry.position) || 0;
  // A saved position past the end is a video whose length was re-encoded, not one
  // to resume: starting at 97% of a different cut shows the credits and stops.
  return position > 0 && position < (Number(entry.duration) || 0) - 5000 ? position : 0;
}

/** How much of this video is left, 0-100, for a progress bar on the card. */
export function watchedPercent(id) {
  const entry = resumeEntries().find((row) => row.id === id);
  if (!entry || !entry.duration) return 0;
  const done = Math.min(1, (Number(entry.position) || 0) / entry.duration);
  // Below the floor it is not part-watched, so nothing is drawn.
  return done < 0.02 ? 0 : Math.round(done * 100);
}

// ---------------------------------------------------------------------------
// Watch later
// ---------------------------------------------------------------------------

export function laterIds() {
  return readList(LATER_KEY).filter((id) => typeof id === 'string');
}

export function isSaved(id) {
  return laterIds().includes(id);
}

export function toggleSaved(id) {
  const ids = laterIds();
  const at = ids.indexOf(id);
  if (at >= 0) ids.splice(at, 1);
  else ids.unshift(id);
  writeValue(LATER_KEY, ids.slice(0, LATER_LIMIT));
  return at < 0;
}

// ---------------------------------------------------------------------------
// Poster cache
// ---------------------------------------------------------------------------

/**
 * Posters already drawn, as data URLs.
 *
 * `localStorage` rather than memory: a poster is a few kilobytes of base64, and
 * twenty-four of them fill the shelf for a second session without re-reading the
 * video files. The size is what makes this worth doing - a memory cache would save
 * nothing, because the page that needs it has just been reloaded.
 */
export function posterCache() {
  const raw = readValue(POSTER_KEY, {});
  return raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
}

export function cachedPoster(id) {
  return posterCache()[id] || null;
}

export function cachePoster(id, dataUrl) {
  if (!id || typeof dataUrl !== 'string' || !dataUrl.startsWith('data:image/')) return false;
  const all = posterCache();
  all[id] = dataUrl;
  // Bounded by age rather than by count: keeping the twenty most recent is what
  // makes it useful, and a shelf a reader scrolls back to is worth a re-read.
  const keys = Object.keys(all);
  if (keys.length > 40) {
    for (const stale of keys.slice(40)) delete all[stale];
  }
  return writeValue(POSTER_KEY, all);
}