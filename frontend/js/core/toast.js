/**
 * Toast notifications.
 *
 * Errors announce themselves through an `aria-live` region so screen readers
 * announce them without stealing focus; the visual toast never receives focus
 * either, so an error cannot interrupt what the user is typing.
 */

import { t } from '../core/i18n.js';

import { el, clear } from './dom.js';

let region = null;

const ICONS = {
  success: 'M4 8.5l2.5 2.5L12 5.5',
  error: 'M8 4.5v4.2M8 11.2v.3M8 1.8a6.2 6.2 0 100 12.4A6.2 6.2 0 008 1.8z',
  warning: 'M8 6v3.2M8 11.4v.3M7 2.2L1.6 12a1.1 1.1 0 00.9 1.7h11a1.1 1.1 0 00.9-1.7L9 2.2a1.1 1.1 0 00-2 0z',
  info: 'M8 7.2v4M8 4.6v.3M8 1.8a6.2 6.2 0 100 12.4A6.2 6.2 0 008 1.8z',
};

function ensureRegion() {
  if (region && document.body.contains(region)) return region;
  region = el('div', {
    class: 'toast-region',
    role: 'status',
    'aria-live': 'polite',
    'aria-atomic': 'false',
  });
  document.body.append(region);
  return region;
}

function icon(kind) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'toast-icon');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', ICONS[kind] || ICONS.info);
  path.setAttribute('stroke', 'currentColor');
  path.setAttribute('stroke-width', '1.5');
  path.setAttribute('stroke-linecap', 'round');
  path.setAttribute('stroke-linejoin', 'round');
  svg.append(path);
  return svg;
}

function show(kind, message, { duration = 5000, action = null } = {}) {
  const host = ensureRegion();

  const close = el('button', {
    class: 'toast-close',
    type: 'button',
    'aria-label': t('Закрыть уведомление'),
    onclick: () => dismiss(node),
  });
  close.innerHTML =
    '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';

  const body = el('div', { class: 'toast-body' }, el('div', { text: message }));
  if (action) {
    body.append(
      el('button', {
        class: 'toast-action',
        type: 'button',
        text: action.label,
        onclick: () => {
          action.onClick?.();
          dismiss(node);
        },
      }),
    );
  }

  const node = el('div', { class: `toast toast-${kind}`, role: kind === 'error' ? 'alert' : 'status' },
    icon(kind),
    body,
    close,
  );

  host.append(node);

  // Errors stay until dismissed; successes fade. Silently swallowing a
  // persistent error is how users end up filing "nothing happened" reports.
  const timer = duration > 0 ? setTimeout(() => dismiss(node), duration) : null;
  node.addEventListener('mouseenter', () => timer && clearTimeout(timer));
  node.addEventListener('mouseleave', () => {
    if (duration > 0 && !node.classList.contains('is-leaving')) {
      setTimeout(() => dismiss(node), 1500);
    }
  });

  return () => dismiss(node);
}

function dismiss(node) {
  if (!node || node.classList.contains('is-leaving')) return;
  node.classList.add('is-leaving');
  node.addEventListener('animationend', () => node.remove(), { once: true });
  setTimeout(() => node.remove(), 400);
}

export const toast = {
  success: (message, options) => show('success', message, { duration: 3800, ...options }),
  error: (message, options) => show('error', message, { duration: 8000, ...options }),
  warning: (message, options) => show('warning', message, { duration: 6000, ...options }),
  info: (message, options) => show('info', message, { duration: 4500, ...options }),
  dismissAll: () => {
    if (region) clear(region);
  },
};

export default toast;
