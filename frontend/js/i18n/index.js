/**
 * Language catalogue registry.
 *
 * Each entry pairs a BCP 47 code with its writing direction and a `strings`
 * table keyed by the *Russian source text*. See `core/i18n.js` for why the
 * source text is the key.
 *
 * `rtl` lives here rather than being derived in the client: the direction is a
 * property of the script, and a second hard-coded list of the same fact is a
 * second thing to get wrong.
 *
 * The base language has an empty table on purpose - its translation of every
 * key is the key itself, so there is nothing to list and nothing to fall out of
 * date.
 */

export const BASE_LANGUAGE = 'ru';
export const DEFAULT_LANGUAGE = 'ru';

import uk from './uk.js';
import be from './be.js';
import kk from './kk.js';
import uz from './uz.js';
import ky from './ky.js';
import de from './de.js';
import fr from './fr.js';
import es from './es.js';
import tr from './tr.js';
import ar from './ar.js';
import en from './en.js';

export const CATALOGUES = {
  ru: { code: 'ru', native: 'Русский', english: 'Russian', rtl: false, strings: {} },
  en,
  uk,
  be,
  kk,
  uz,
  ky,
  de,
  fr,
  es,
  tr,
  ar,
};

/** Ordered for the settings picker: the language's own name, as its speakers write it. */
export const LOCALE_LIST = Object.values(CATALOGUES)
  .map(({ code, native, english, rtl }) => ({ code, native, english, rtl }))
  .sort((a, b) => a.native.localeCompare(b.native, undefined, { numeric: true }));

export function isSupported(code) {
  return Boolean(code) && Object.prototype.hasOwnProperty.call(CATALOGUES, normalise(code));
}

/** Reduce a BCP 47 tag to a catalogue we hold, e.g. `uk-UA` -> `uk`. */
export function normalise(tag) {
  if (!tag) return null;
  const lower = String(tag).toLowerCase().replace(/_/g, '-');
  if (Object.prototype.hasOwnProperty.call(CATALOGUES, lower)) return lower;
  const primary = lower.split('-', 1)[0];
  return Object.prototype.hasOwnProperty.call(CATALOGUES, primary) ? primary : null;
}

/**
 * How much of the interface a language covers, as a percentage of the keys the
 * base language actually uses.
 *
 * Shown in the picker. A reader deserves to know before they switch whether
 * they are getting the whole interface or most of it, rather than finding out
 * from the page itself.
 */
export function coverage(code, baseKeys) {
  const table = CATALOGUES[normalise(code) || BASE_LANGUAGE]?.strings || {};
  if (!baseKeys || !baseKeys.length) return 100;
  const known = baseKeys.filter((key) => Object.prototype.hasOwnProperty.call(table, key)).length;
  return Math.round((known / baseKeys.length) * 100);
}
