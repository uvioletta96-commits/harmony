/**
 * Interface translation.
 *
 * **The key is the source string.** A lookup is `t('Лента')` -> a translation
 * of that Russian text. This has a property worth more than a tidier key
 * namespace: the base language is complete by construction, because the key *is*
 * its own translation. There is no such thing as a missing base key, so a string
 * nobody got round to translating still renders as readable Russian rather than
 * as a blank space or a raw identifier.
 *
 * It also means adding a feature never needs a translation pass to avoid
 * breaking existing ones. A new string simply falls back to Russian on day one
 * and is picked up by the catalogue the next time it is touched.
 *
 * **Resolution order** for the active language:
 *
 *   1. a choice the reader made in this browser (`localStorage`);
 *   2. the value stored on their account, when signed in;
 *   3. `navigator.languages`, in order;
 *   4. the base language.
 *
 * Step 3 is why a visitor with a Ukrainian browser sees Ukrainian before they
 * have an account. It is not written back anywhere: a language nobody picked is
 * a guess, and a guess is re-made each visit rather than becoming a fact about
 * the reader.
 *
 * **Every language in the catalogue is translated in full.** A short list you
 * can actually read beats a long one you have to guess at, and half of it being
 * in the wrong language is worse than not offering it.
 */

import { CATALOGUES, BASE_LANGUAGE, DEFAULT_LANGUAGE } from '../i18n/index.js';

let strings = {};
let active = DEFAULT_LANGUAGE;
let direction = 'ltr';
let ready = false;
let listeners = [];
const reported = new Set();

/** Read the reader's own choice for this browser. */
function storedChoice() {
  try {
    return window.localStorage.getItem('harmony_lang');
  } catch {
    // Private browsing and blocked storage both throw here. Neither is a reason
    // to fail the app, so the guess path takes over.
    return null;
  }
}

function rememberChoice(code) {
  try {
    window.localStorage.setItem('harmony_lang', code);
  } catch {
    /* A preference that cannot be stored still applies for this session. */
  }
}

/**
 * Reduce a BCP 47 tag to a catalogue we hold, e.g. `uk-UA` -> `uk`.
 *
 * Returns null on a miss rather than a guess, so "no such language" can be told
 * apart from "the base language".
 */
export function normalise(tag) {
  if (!tag) return null;
  const lower = String(tag).toLowerCase().replace(/_/g, '-');
  if (CATALOGUES[lower]) return lower;
  const primary = lower.split('-', 1)[0];
  return CATALOGUES[primary] ? primary : null;
}

/** Pick a catalogue for a list of browser preferences. */
export function detect(languages) {
  const list = languages || (typeof navigator !== 'undefined' ? navigator.languages : null) || [];
  for (const tag of list) {
    const code = normalise(tag);
    if (code) return code;
  }
  return DEFAULT_LANGUAGE;
}

export function supported() {
  return Object.keys(CATALOGUES);
}

export function current() {
  return active;
}

export function currentDirection() {
  return direction;
}

export function isReady() {
  return ready;
}

function apply(code) {
  active = code;
  const catalogue = CATALOGUES[code] || CATALOGUES[BASE_LANGUAGE];
  strings = catalogue.strings || {};
  direction = catalogue.rtl ? 'rtl' : 'ltr';

  if (typeof document !== 'undefined' && document.documentElement) {
    document.documentElement.lang = code;
    document.documentElement.dir = direction;
  }
  for (const listener of listeners) listener(code);
}

/**
 * Bring the interface up in the right language.
 *
 * @param {string|null} preference the signed-in reader's stored account value
 */
export function init(preference) {
  apply(normalise(preference) || normalise(storedChoice()) || detect());
  ready = true;
  return active;
}

/** Switch language for this browser and remember it here. */
export function setLanguage(code) {
  const target = normalise(code);
  if (!target) return active;
  rememberChoice(target);
  apply(target);
  return active;
}

/** Re-resolve after sign-in or sign-out, when the account value changes. */
export function syncWithProfile(preference) {
  apply(normalise(preference) || detect());
  return active;
}

export function onChange(listener) {
  listeners.push(listener);
  return () => {
    listeners = listeners.filter((entry) => entry !== listener);
  };
}

/**
 * Translate a key.
 *
 * `values` fills `{name}` placeholders. The result is plain text, never markup:
 * a translation is content from outside the code, and post bodies staying text
 * is the one property this project does not compromise on.
 */
export function t(key, values) {
  let value = strings[key];
  // `!value` rather than `=== undefined`: an empty string in a catalogue is an
  // untranslated placeholder, and rendering it would produce a blank button.
  // A missing key is a normal state here, not an error - the base language is
  // the key itself.
  if (!value) {
    if (active !== BASE_LANGUAGE && !reported.has(key)) {
      reported.add(key);
      console.warn(`[i18n] "${key}" has no ${active} translation yet; showing Russian.`);
    }
    value = key;
  }
  if (!values) return value;
  return value.replace(/\{(\w+)\}/g, (match, name) =>
    Object.prototype.hasOwnProperty.call(values, name) ? String(values[name]) : match,
  );
}

const i18n = {
  t,
  init,
  setLanguage,
  syncWithProfile,
  detect,
  current,
  currentDirection,
  isReady,
  onChange,
  supported,
  normalise,
};

export default i18n;
