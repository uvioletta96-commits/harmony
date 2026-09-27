/** 404 page. */

import { t } from '../core/i18n.js';

import { el } from '../core/dom.js';
import { button, emptyState } from '../components/ui.js';
import { router } from '../core/router.js';

export async function render() {
  document.title = t('Страница не найдена · Гармония');
  return {
    node: el('div', { class: 'layout-center', style: { margin: '0 auto', paddingTop: 'var(--space-8)' } },
      emptyState({
        iconName: 'search',
        title: t('Такой страницы нет'),
        text: t('Возможно, ссылка устарела или в адресе опечатка.'),
        action: el('div', { style: { display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap', justifyContent: 'center' } },
          button(t('На главную'), { variant: 'primary', onClick: () => router.navigate('/') }),
          button(t('Поиск'), { variant: 'secondary', onClick: () => router.navigate('/search') }),
        ),
      }),
    ),
  };
}
