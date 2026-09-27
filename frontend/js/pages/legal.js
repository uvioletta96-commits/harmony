/**
 * Legal pages (terms of service, privacy policy).
 *
 * Content is served from the same documents the backend stores and hashes for
 * consent records, so what a user reads is exactly what they agreed to.
 */

import { t } from '../core/i18n.js';
import { locale } from '../core/format.js';

import api from '../core/api.js';
import { el, clear } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { button, loadingRow, errorState, alert } from '../components/ui.js';
import store from '../core/store.js';

export async function renderTerms() {
  return renderDocument('terms', t('Пользовательское соглашение'));
}

export async function renderPrivacy() {
  return renderDocument('privacy', t('Политика конфиденциальности'));
}

/**
 * Let a wide table scroll instead of pushing the page sideways.
 *
 * The retention and lawful-basis tables are three columns of prose. A table
 * will not shrink below the width its widest cell needs, so inside a reading
 * column it overflowed the card and dragged the whole layout with it - 19px of
 * horizontal scroll on the privacy policy and nothing else.
 *
 * A wrapper is the fix rather than `display: block` on the table, which would
 * make every row its own block and lose the column alignment that makes a table
 * readable in the first place.
 */
function wrapWideTables(body) {
  for (const table of body.querySelectorAll('table')) {
    const scroller = el('div', { class: 'table-scroll', tabindex: '0', role: 'region' });
    table.replaceWith(scroller);
    scroller.append(table);
  }
}

async function renderDocument(slug, fallbackTitle) {
  document.title = t('{v0} · Гармония', { v0: fallbackTitle });
  const body = el('div', { class: 'legal-body' });
  const meta = el('p', { class: 'legal-meta', text: t('Загружаем…') });
  const shell = el('div', { class: 'legal-wrap' },
    el('div', { class: 'legal-head' },
      el('a', { class: 'btn btn-ghost btn-sm', href: '/', style: { marginBottom: 'var(--space-3)' } },
        icon('chevronLeft', { size: 15 }), t(' На главную')),
      el('h1', { class: 'legal-title', text: fallbackTitle }),
      meta,
    ),
    loadingRow(),
    body,
  );

  try {
    // Named `legal`, not `document`: destructuring into `document` shadows
    // the global for the rest of the function, and this file needs both the
    // API payload and the real DOM.
    const { document: legal } = await api.get(`/legal/documents/${slug}`);
    shell.querySelector('.loading-row')?.remove();
    meta.textContent =
      t('Версия {v0} · в силе с {v1}', {
        v0: legal.version,
        v1: new Date(legal.effective_from).toLocaleDateString(locale()),
      });

    // The document is sanitised server-side (script/style/event handlers and
    // dangerous URL schemes removed) before it is stored, and is rendered with
    // innerHTML here because it is authored by the operator, not by users.
    body.innerHTML = legal.content;
    wrapWideTables(body);

    if (store.get('currentUser')) {
      body.insertBefore(
        alert({
          variant: 'info',
          text: t('Это текущая версия документа, с которой вы согласились. Все предыдущие версии доступны в истории согласий в настройках профиля.'),
        }),
        body.firstChild,
      );
    }
  } catch (error) {
    clear(shell);
    shell.append(errorState({ text: error.message }));
  }

  return { node: shell };
}
