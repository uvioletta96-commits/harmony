/**
 * Formatting helpers.
 *
 * Dates are rendered as relative time (t("5 минут назад")) and upgraded to an
 * absolute timestamp on hover or long-press. Relative time reads faster in a
 * feed; the absolute value is what someone actually needs when they are
 * checking whether something happened before or after an event.
 */

import { t, current as currentLanguage } from '../core/i18n.js';

const MINUTE = 60;
const HOUR = 3600;
const DAY = 86400;
const WEEK = 7 * DAY;

/**
 * Plural category for the active language, via `Intl.PluralRules`.
 *
 * The hand-written rule this replaces was Russian-specific and always applied,
 * which meant an English reader was shown "1 hours" and Arabic - which has six
 * categories, not three - had no correct form available at all. The platform
 * already knows these rules for every language we ship, and for locales we have
 * not heard of, so the table is deleted rather than extended.
 *
 * `forms` is base language first, then the remaining categories in the order
 * below, so a two-form list stays readable:
 *
 *   plural(2, ['минута', 'минуты', 'минуты', 'минуты', 'минут', 'минуты'])
 *   plural(2, ['hour', 'hours'])
 *
 * A catalogue for a language with more categories supplies more slots; one with
 * fewer supplies fewer, and a missing slot falls back to the last form given.
 * The order is by how often each category is reached in the languages we ship,
 * so the common cases are the readable ones.
 */
const CATEGORIES = ['one', 'few', 'many', 'two', 'other'];

let pluralRules = null;
let pluralRulesFor = null;

function rules() {
  const language = currentLanguage();
  if (pluralRulesFor !== language) {
    try {
      pluralRules = new Intl.PluralRules(language);
    } catch {
      // An engine without full ICU. `other` is a wrong answer for some counts,
      // but throwing would take every timestamp in the feed with it.
      pluralRules = { select: () => 'other' };
    }
    pluralRulesFor = language;
  }
  return pluralRules;
}

export function plural(count, forms) {
  const list = Array.isArray(forms) ? forms : [forms];
  let category = rules().select(Math.abs(Number(count) || 0));
  if (!CATEGORIES.includes(category)) category = 'other';
  const index = CATEGORIES.indexOf(category);
  return list[index] ?? list[list.length - 1] ?? String(count);
}

/**
 * The index of the plural form to use, for a UI with exactly three forms.
 *
 * Exists because `plural()` returns a string and the vertical feed needs the
 * index: each of its forms has to be a whole translatable *phrase*, not a noun
 * with the number prepended. Assembling `${n} ${plural(n, ['comment', ...])}`
 * fixes the word order for one language and breaks it for the others - in Arabic
 * the number follows the noun, and nothing can reorder it afterwards.
 *
 * CLDR names six categories and this UI has three forms, so the mapping is
 * `zero`/`one` -> 0, `two`/`few`/`many` -> 1, everything else -> 2. The same
 * collapse `plural()` performs when a language names fewer forms than are
 * supplied, so the two agree about which form was picked.
 */
export function pluralIndex(count, formCount = 3) {
  let category = rules().select(Math.abs(Number(count) || 0));
  if (!CATEGORIES.includes(category)) category = 'other';
  const collapsed = CATEGORIES.indexOf(category);
  const buckets = formCount === 2 ? [[0, 1], [5]] : [[0, 1], [2, 3, 4], [5]];
  const index = buckets.findIndex((bucket) => bucket.includes(collapsed));
  return index < 0 ? formCount - 1 : Math.min(index, formCount - 1);
}

/** The active language as a BCP 47 tag, for the Intl date formatters. */
export function locale() {
  return currentLanguage();
}

/** Compact counts: 1200 -> t("1,2 тыс.") */
export function compactNumber(value) {
  const number = Number(value) || 0;
  if (number < 1000) return String(number);
  if (number < 1_000_000) {
    const value = number / 1000;
    return t('{v0} тыс.', { v0: value < 10 ? value.toFixed(1).replace('.0', '') : Math.round(value) });
  }
  return t('{v0} млн', { v0: (number / 1_000_000).toFixed(1).replace('.0', '') });
}

/**
 * Milliseconds as `m:ss`, or `h:mm:ss` past an hour.
 *
 * Two different jobs use this - a player, which wants precision, and a card badge,
 * which wants to fit - so the caller chooses the format rather than this guessing
 * from context it does not have.
 */
export function formatDuration(ms) {
  const total = Math.max(0, Math.round((Number(ms) || 0) / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) return `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

export function parseDate(value) {
  if (!value) return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function relativeTime(value, now = Date.now()) {
  const date = parseDate(value);
  if (!date) return '';

  const seconds = Math.floor((now - date.getTime()) / 1000);
  if (seconds < 45) return t('только что');
  if (seconds < 90) return t('минуту назад');

  const minutes = Math.floor(seconds / MINUTE);
  if (minutes < 60) return t('{v0} {v1} назад', {v0: minutes, v1: plural(minutes, ['минуту', 'минуты', 'минут']) });

  const hours = Math.floor(seconds / HOUR);
  if (hours < 24) return t('{v0} {v1} назад', {v0: hours, v1: plural(hours, ['час', 'часа', 'часов']) });

  const days = Math.floor(seconds / DAY);
  if (days === 1) return t('вчера');
  if (days < 7) return t('{v0} {v1} назад', {v0: days, v1: plural(days, ['день', 'дня', 'дней']) });

  const weeks = Math.floor(days / 7);
  if (weeks < 5) return t('{v0} {v1} назад', {v0: weeks, v1: plural(weeks, ['неделю', 'недели', 'недель']) });

  return absoluteDate(date);
}

export function absoluteDate(value) {
  const date = parseDate(value);
  if (!date) return '';

  const now = new Date();
  const sameYear = date.getFullYear() === now.getFullYear();
  return date.toLocaleDateString(locale(), {
    day: 'numeric',
    month: 'long',
    ...(sameYear ? {} : { year: 'numeric' }),
  });
}

export function absoluteTime(value) {
  const date = parseDate(value);
  if (!date) return '';
  return date.toLocaleString(locale(), {
    day: 'numeric',
    month: 'long',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** t("сегодня в 14:32") / t("вчера в 09:15") / absolute */
export function smartDate(value) {
  const date = parseDate(value);
  if (!date) return '';
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const dayDiff = Math.floor((startOfToday - new Date(date.getFullYear(), date.getMonth(), date.getDate())) / DAY);

  const time = date.toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' });
  if (dayDiff === 0) return t('сегодня в {v0}', { v0: time });
  if (dayDiff === 1) return t('вчера в {v0}', { v0: time });
  return t('{v0} в {v1}', {v0: absoluteDate(date), v1: time });
}

export function dayLabel(value) {
  const date = parseDate(value);
  if (!date) return '';
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const dayDiff = Math.floor((startOfToday - new Date(date.getFullYear(), date.getMonth(), date.getDate())) / DAY);
  if (dayDiff === 0) return t('Сегодня');
  if (dayDiff === 1) return t('Вчера');
  if (dayDiff < 7) return date.toLocaleDateString(locale(), { weekday: 'long' });
  return absoluteDate(date);
}

export function bytes(value) {
  const size = Number(value) || 0;
  if (size < 1024) return t('{v0} Б', { v0: size });
  if (size < 1024 * 1024) return t('{v0} КБ', { v0: (size / 1024).toFixed(0) });
  return t('{v0} МБ', { v0: (size / (1024 * 1024)).toFixed(1) });
}

export function initialsOf(name) {
  const parts = String(name || '').trim().split(/[\s._-]+/).filter(Boolean);
  if (!parts.length) return '?';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

/** Deterministic avatar colour so a user always gets the same neutral tone. */
const AVATAR_COLOURS = ['sand', 'stone', 'sage', 'clay', 'dusk', 'linen', 'moss', 'ash'];

export function colourFor(seed) {
  const text = String(seed || '');
  let hash = 0;
  for (let index = 0; index < text.length; index += 1) {
    hash = (hash * 31 + text.charCodeAt(index)) >>> 0;
  }
  return AVATAR_COLOURS[hash % AVATAR_COLOURS.length];
}

export function truncate(text, limit) {
  const value = String(text ?? '');
  if (value.length <= limit) return value;
  return `${value.slice(0, limit - 1).trimEnd()}…`;
}

export function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
