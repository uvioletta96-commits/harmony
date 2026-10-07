/**
 * Small persistent values: a reader's own choices, kept in this browser.
 *
 * `localStorage` rather than the app store, because these have to outlive a reload
 * and the store is in-memory. That distinction was learned the hard way - two
 * preferences were written with `store.set('key', value)` while `store.set` takes
 * an object, so `Object.entries` walked the *string's* indices and both calls
 * silently did nothing. The chrome toggle and the immersive mode were written,
 * read back as `undefined`, and had never once worked.
 *
 * Everything here fails soft. `localStorage` throws in private mode and when the
 * quota is full, and a preference that cannot be saved must not take the page down
 * with it - the reader gets the feature for this visit and loses it on reload,
 * which is a far better outcome than an exception on every tap.
 */

const PREFIX = 'harmony:';

/** The store, or null. Probed once: a write probe is the only reliable test. */
let backing;
let probed = false;

function storage() {
  if (!probed) {
    probed = true;
    try {
      const probe = `${PREFIX}__probe__`;
      window.localStorage.setItem(probe, '1');
      window.localStorage.removeItem(probe);
      backing = window.localStorage;
    } catch {
      backing = null;
    }
  }
  return backing;
}

/** Read one value, or `fallback`. Never throws, whatever is in the slot. */
export function readValue(key, fallback = null) {
  const store = storage();
  if (!store) return fallback;
  try {
    const raw = store.getItem(PREFIX + key);
    if (raw === null) return fallback;
    return JSON.parse(raw);
  } catch {
    // Corrupt rather than absent: a truncated write, or something else on this
    // origin wrote here. Treat it as unset rather than failing.
    return fallback;
  }
}

/** Write one value. Returns whether it was saved, so a caller can say so. */
export function writeValue(key, value) {
  const store = storage();
  if (!store) return false;
  try {
    store.setItem(PREFIX + key, JSON.stringify(value));
    return true;
  } catch {
    // Quota, most often. One more attempt without the values we can regenerate:
    // caches are disposable, and a full quota should not stop a preference.
    try {
      store.removeItem(`${PREFIX}posters`);
    } catch {
      /* nothing more to try */
    }
    try {
      store.setItem(PREFIX + key, JSON.stringify(value));
      return true;
    } catch {
      return false;
    }
  }
}

/** Read a boolean choice, defaulting to `fallback` when unset. */
export function readFlag(key, fallback = false) {
  const value = readValue(key, null);
  return typeof value === 'boolean' ? value : fallback;
}

/** Read a list, dropping anything that is not an array. */
export function readList(key) {
  const value = readValue(key, []);
  return Array.isArray(value) ? value : [];
}