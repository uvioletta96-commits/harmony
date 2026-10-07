/**
 * Application shell: top bar, desktop sidebar, right rail, mobile nav.
 *
 * The shell is built once and kept across navigations. Only the centre column
 * is replaced by the router, which keeps the scroll position of the rails and
 * avoids a full re-layout on every route change.
 */

import { t } from '../core/i18n.js';

import { el, clear, $ } from '../core/dom.js';
import { icon, brandMark } from '../core/icons.js';
import store, { logout } from '../core/store.js';
import { router } from '../core/router.js';
import { avatar, iconButton, menu } from './ui.js';
import { openComposer } from './composer.js';
import { hasDraft } from '../core/drafts.js';
import { compactNumber } from '../core/format.js';

const NAV = [
  { href: '/', label: t('Лента'), icon: 'home', key: null },
  { href: '/videos', label: t('Клипы'), icon: 'play', key: null },
  { href: '/efir', label: t('Эфир'), icon: 'film', key: null },
  { href: '/search', label: t('Поиск'), icon: 'search', key: null },
  { href: '/notifications', label: t('Уведомления'), icon: 'bell', key: 'notifications' },
  { href: '/chat', label: t('Сообщения'), icon: 'mail', key: 'messages' },
  { href: '/settings', label: t('Настройки'), icon: 'settings', key: null },
];

// Deliberately four items, not five: the bar has room for four comfortably at
// 360px, and a fifth wraps or shrinks the labels until they are unreadable. Both
// video sections are reachable from the feed's own header and from this bar on
// wider screens.
const MOBILE_NAV = [
  { href: '/', label: t('Лента'), icon: 'home', key: null },
  { href: '/videos', label: t('Клипы'), icon: 'play', key: null },
  { href: '/chat', label: t('Сообщения'), icon: 'mail', key: 'messages' },
  { href: '/settings', label: t('Профиль'), icon: 'user', key: null },
];

export function renderShell() {
  const shell = el('div', { class: 'app' });
  shell.append(buildTopbar(), buildLayout(), buildMobileNav(), buildFab());
  wireActiveState(shell);
  return shell;
}

/* -------------------------------------------------------------------------- */
/* Top bar                                                                     */
/* -------------------------------------------------------------------------- */

function buildTopbar() {
  const search = el('form', {
    class: 'topbar-search search-wrap',
    role: 'search',
    onsubmit: (event) => {
      event.preventDefault();
      const value = searchInput.value.trim();
      if (value) router.navigate(`/search?q=${encodeURIComponent(value)}`);
    },
  });
  const searchInput = el('input', {
    class: 'input',
    type: 'search',
    name: 'q',
    placeholder: t('Поиск людей и публикаций'),
    'aria-label': t('Поиск'),
    autocomplete: 'off',
  });
  search.append(el('span', { class: 'search-icon' }, icon('search')), searchInput);

  const actions = el('div', { class: 'topbar-actions' });

  /**
   * The action area, redrawn whenever who is signed in or what is unread
   * changes.
   *
   * It used to be built once, from whatever ``currentUser`` happened to be at
   * boot - and the shell is built *before* the session is resolved, so a
   * signed-out reader kept seeing "Sign in" and "Register" for the whole time
   * even after signing in. The same code also left a signed-in reader's avatar
   * menu missing on a hard refresh, because the subscription that would have
   * drawn it was only installed in the branch that already knew about a user.
   */
  const renderActions = () => {
    clear(actions);
    // The shell is built before the session has been resolved, so for one frame
    // `currentUser` is null no matter who is reading. Drawing "Sign in" and
    // "Register" from that null is what made a signed-in reader see the
    // signed-out chrome on every full page load, and then have it change under
    // them a moment later. An empty action area for that one frame is honest;
    // the wrong buttons are not.
    if (!store.get('authResolved')) return;

    const current = store.get('currentUser');
    if (!current) {
      actions.append(
        el('a', { class: 'btn btn-ghost btn-sm', href: '/login', text: t('Войти') }),
        el('a', { class: 'btn btn-primary btn-sm', href: '/register', text: t('Регистрация') }),
      );
      return;
    }

    const counters = store.get('notifications') || { unread: 0 };
    // First, not last: posting is the thing people came here to do.
    actions.append(
      composeButton(),
      iconButton('bell', {
        title: t('Уведомления'),
        badge: counters.unread,
        onClick: () => router.navigate('/notifications'),
      }),
    );
    if (current.role === 'moderator' || current.role === 'admin') {
      actions.append(iconButton('shield', { title: t('Панель модератора'), onClick: () => router.navigate('/admin') }));
    }
    actions.append(userMenu(current));
  };

  renderActions();
  store.subscribe('currentUser', renderActions);
  store.subscribe('notifications', renderActions);
  store.subscribe('logged-out', renderActions);
  // The one that decides whether to draw anything at all.
  store.subscribe('authResolved', renderActions);

  return el('header', { class: 'topbar' },
    el('div', { class: 'topbar-inner' },
      el('a', { class: 'brand', href: '/', 'aria-label': t('Гармония — на главную') },
        brandMark({ size: 26 }),
        el('span', { class: 'brand-word' }, t('Гармония')),
      ),
      search,
      actions,
    ),
  );
}

/**
 * Open the composer, wherever the reader happens to be.
 *
 * The feed's composer is the only one on the page, so a button on any other
 * route has to bring the reader to it. Going to the feed and focusing the field
 * is honest about what is happening and reuses the real form; opening a second
 * composer here would mean two composers with the same attachment and
 * character state, and they would disagree.
 */
/**
 * Open the composer, wherever the reader is.
 *
 * This used to navigate to the feed and focus the composer that lived there,
 * which meant a button in the top bar threw away whatever page you were on and
 * replaced it with a form. The composer is a dialog now and this opens it.
 */
function goCompose() {
  openComposer();
}

function composeButton({ compact = false } = {}) {
  const pending = hasDraft();
  const label = pending ? t('Написать') : t('Написать');
  return el('button', {
    class: compact ? 'icon-btn compose-btn' : 'btn btn-primary compose-btn',
    type: 'button',
    'aria-label': pending ? t('Написать — есть черновик') : t('Написать публикацию'),
    title: pending ? t('Написать — есть черновик') : t('Написать публикацию'),
    onclick: goCompose,
  },
    icon('plus', { size: compact ? 20 : 18 }),
    compact ? null : el('span', { text: label }),
    // A dot rather than a second line of text: the button is in a top bar that
    // is already narrow, and "you have unsent text" is a glance, not a sentence.
    pending ? el('span', { class: 'compose-dot', 'aria-hidden': 'true' }) : null,
  );
}

function userMenu(user) {
  const wrap = el('div', { class: 'dropdown' });
  const trigger = el('button', {
    class: 'icon-btn',
    type: 'button',
    'aria-haspopup': 'menu',
    'aria-expanded': 'false',
    'aria-label': t('Меню профиля'),
    style: { padding: '0' },
  }, avatar(user, { size: 'sm', link: false }));

  const items = [
    { label: t('Профиль'), icon: 'user', onClick: () => router.navigate(`/u/${user.username}`) },
    { label: t('Настройки'), icon: 'settings', onClick: () => router.navigate('/settings') },
    '-',
    { label: t('Выйти'), icon: 'logout', danger: true, onClick: () => logout() },
  ];

  const list = el('div', { class: 'dropdown-menu', role: 'menu' });
  list.append(el('div', { class: 'dropdown-label' }, `@${user.username}`), el('div', { class: 'dropdown-separator' }));
  for (const item of items) {
    if (item === '-') {
      list.append(el('div', { class: 'dropdown-separator' }));
      continue;
    }
    list.append(el('button', {
      class: `dropdown-item ${item.danger ? 'dropdown-item-danger' : ''}`,
      type: 'button',
      role: 'menuitem',
      onclick: () => {
        close();
        item.onClick();
      },
    }, icon(item.icon, { size: 16 }), el('span', { text: item.label })));
  }

  let open = false;
  const close = () => {
    open = false;
    list.style.display = 'none';
    trigger.setAttribute('aria-expanded', 'false');
    document.removeEventListener('click', onOutside, true);
    document.removeEventListener('keydown', onKey, true);
  };
  const onOutside = (event) => {
    if (!wrap.contains(event.target)) close();
  };
  const onKey = (event) => event.key === 'Escape' && close();

  trigger.addEventListener('click', (event) => {
    event.stopPropagation();
    open = !open;
    list.style.display = open ? 'block' : 'none';
    trigger.setAttribute('aria-expanded', String(open));
    if (open) {
      setTimeout(() => {
        document.addEventListener('click', onOutside, true);
        document.addEventListener('keydown', onKey, true);
      }, 0);
    }
  });

  list.style.display = 'none';
  wrap.append(trigger, list);
  return wrap;
}

/* -------------------------------------------------------------------------- */
/* Layout                                                                      */
/* -------------------------------------------------------------------------- */

function buildLayout() {
  // The router writes here. The id is the contract: ``core/router.js`` looks
  // for ``main#main`` first, and falls back to ``#app`` only when a page is
  // rendered without a shell around it.
  const main = el('main', { class: 'layout-main', id: 'main', tabindex: '-1' });
  const left = el('aside', { class: 'layout-left' });
  const right = el('aside', { class: 'layout-right' });

  buildSidebar(left);
  buildRail(right);

  return el('div', { class: 'layout' }, left, main, right);
}

function buildSidebar(host) {
  const user = store.get('currentUser');
  const nav = el('nav', { class: 'side-nav', 'aria-label': t('Основная навигация') });

  const render = () => {
    clear(nav);
    const current = store.get('currentUser');
    const counters = store.get('notifications') || { unread: 0, messages: 0 };

    for (const item of NAV) {
      nav.append(
        el('a', { class: 'side-link', href: item.href, 'data-nav': item.href },
          icon(item.icon, { size: 20 }),
          el('span', { text: item.label }),
          item.key && counters[item.key]
            ? el('span', { class: 'badge-count side-badge', text: String(counters[item.key]) })
            : null,
        ),
      );
    }
    if (current && (current.role === 'moderator' || current.role === 'admin')) {
      nav.append(
        el('div', { class: 'side-section' }, t('Администрирование')),
        el('a', { class: 'side-link', href: '/admin', 'data-nav': '/admin' },
          icon('shield', { size: 20 }),
          el('span', { text: t('Модерация') }),
        ),
      );
    }
    // Same reason as the topbar: an unresolved session is not a signed-out one,
    // and the account section is the part of the sidebar that lies about it
    // most visibly - "Sign in" and "Create an account" appearing for a reader
    // who is already signed in, and then vanishing.
    if (current) {
      nav.append(
        el('div', { class: 'side-section' }, t('Аккаунт')),
        el('a', { class: 'side-link', href: `/u/${current.username}`, 'data-nav': 'profile' },
          avatar(current, { size: 'xs', link: false }),
          el('span', { text: '@' + current.username }),
        ),
      );
    } else if (store.get('authResolved')) {
      nav.append(
        el('div', { class: 'side-section' }, t('Аккаунт')),
        el('a', { class: 'side-link', href: '/login', 'data-nav': '/login' }, icon('logout', { size: 20 }), el('span', { text: t('Войти') })),
        el('a', { class: 'side-link', href: '/register', 'data-nav': '/register' }, icon('plus', { size: 20 }), el('span', { text: t('Создать аккаунт') })),
      );
    }
  };

  render();
  store.subscribe('currentUser', render);
  store.subscribe('notifications', render);
  // `set({ currentUser, authResolved })` emits `currentUser` first, so a render
  // triggered by the user is still looking at an unresolved session and draws
  // nothing. Without this subscription the account section would never appear
  // at all on a full page load.
  store.subscribe('authResolved', render);
  host.append(nav);

  if (user) {
    host.append(
      el('div', { class: 'side-card' },
        el('p', { class: 'side-card-title' }, t('О сообществе')),
        el('p', { class: 'hint', style: { marginBottom: 'var(--space-3)' } },
          t('Уважайте других. Споры решаются в диалоге, а не в жалобах.'),
        ),
        el('a', { class: 'btn btn-secondary btn-sm btn-block', href: '/terms', text: t('Правила') }),
        el('a', { class: 'btn btn-ghost btn-sm btn-block', href: '/privacy', text: t('Конфиденциальность'), style: { marginTop: 'var(--space-1)' } }),
      ),
    );
  }
}

function buildRail(host) {
  const rail = el('div', { class: 'rail' });

  const tagsCard = el('div', { class: 'rail-card' },
    el('div', { class: 'rail-head' }, t('Обсуждаемое')),
    el('div', { class: 'rail-body' },
      el('div', { class: 'tag-cloud', id: 'trending-tags' },
        el('span', { class: 'text-muted text-sm' }, t('Загружаем…')),
      ),
    ),
  );

  const peopleCard = el('div', { class: 'rail-card' },
    el('div', { class: 'rail-head' }, t('Кого почитать')),
    el('div', { class: 'rail-body', id: 'rail-people' },
      el('span', { class: 'text-muted text-sm' }, t('Загружаем…')),
    ),
  );

  const footer = el('div', { class: 'rail-card' },
    el('div', { class: 'rail-footer' },
      el('a', { href: '/terms', text: t('Правила') }), ' · ',
      el('a', { href: '/privacy', text: t('Конфиденциальность') }), ' · ',
      el('a', { href: '/api/docs', text: 'API' }),
      el('div', { style: { marginTop: 'var(--space-2)' } }, t('© Гармония')),
    ),
  );

  rail.append(tagsCard, peopleCard, footer);
  host.append(rail);

  // Lazy-load rail content after first paint so it never delays the feed.
  const load = async () => {
    try {
      const data = await import('../core/api.js').then((module) => module.default.get('/users/discover'));
      const tags = data.posts?.length ? extractTags(data.posts) : [];
      const tagHost = document.getElementById('trending-tags');
      if (tagHost) {
        clear(tagHost);
        if (tags.length) {
          for (const tag of tags) {
            tagHost.append(el('a', { class: 'tag', href: `/search?q=${encodeURIComponent(tag.tag)}` },
              `#${tag.tag}`,
              el('span', { class: 'count', text: String(tag.count) }),
            ));
          }
        } else {
          tagHost.append(el('span', { class: 'text-muted text-sm' }, t('Пока пусто')));
        }
      }

      const peopleHost = document.getElementById('rail-people');
      if (peopleHost) {
        clear(peopleHost);
        if (data.people?.length) {
          for (const person of data.people.slice(0, 4)) {
            peopleHost.append(
              el('a', { class: 'rail-person', href: `/u/${person.username}` },
                avatar(person, { size: 'sm', link: false, showOnline: true }),
                el('div', { style: { minWidth: '0' } },
                  el('div', { class: 'name truncate', text: person.display_name || person.username }),
                  el('div', { class: 'meta truncate', text: t('{v0} подписчиков', { v0: compactNumber(person.followers_count || 0) }) }),
                ),
              ),
            );
          }
        } else {
          peopleHost.append(el('span', { class: 'text-muted text-sm' }, t('Подпишитесь на кого-нибудь')));
        }
      }
    } catch {
      // The rail is supplementary; if it fails the feed is unaffected.
      document.getElementById('trending-tags')?.replaceChildren(
        el('span', { class: 'text-muted text-sm' }, t('Недоступно')),
      );
    }
  };

  setTimeout(load, 400);
}

function extractTags(posts) {
  const counts = new Map();
  const pattern = /(?:^|\s)#([\wа-яё]{2,40})/giu;
  for (const post of posts) {
    for (const match of String(post.body || '').matchAll(pattern)) {
      const tag = match[1].toLowerCase();
      counts.set(tag, (counts.get(tag) || 0) + 1);
    }
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8)
    .map(([tag, count]) => ({ tag, count }));
}

/* -------------------------------------------------------------------------- */
/* Mobile navigation                                                           */
/* -------------------------------------------------------------------------- */

function buildMobileNav() {
  const nav = el('nav', { class: 'mobile-nav', 'aria-label': t('Мобильная навигация') });
  const inner = el('div', { class: 'mobile-nav-inner' });

  const render = () => {
    clear(inner);
    const counters = store.get('notifications') || { unread: 0, messages: 0 };
    const current = store.get('currentUser');
    for (const item of MOBILE_NAV) {
      // The profile entry shows the reader's own face rather than a glyph, and
      // the settings entry is kept alongside it: replacing one with the other
      // meant the only way to reach preferences on a phone was through the
      // profile page, which is two taps for something people change often.
      if (item.href === '/settings' && current) {
        inner.append(
          el('a', { class: 'mobile-nav-link', href: `/u/${current.username}`, 'data-nav': 'profile' },
            avatar(current, { size: 'xs', link: false }),
            el('span', { text: t('Профиль') }),
          ),
          el('a', { class: 'mobile-nav-link', href: '/settings', 'data-nav': 'settings' },
            icon('settings', { size: 21 }),
            el('span', { text: t('Настройки') }),
          ),
        );
        continue;
      }
      inner.append(
        el('a', { class: 'mobile-nav-link', href: item.href, 'data-nav': item.href },
          icon(item.icon, { size: 21 }),
          el('span', { text: item.label }),
          item.key && counters[item.key]
            ? el('span', { class: 'badge-count', text: String(counters[item.key]) })
            : null,
        ),
      );
    }
  };

  render();
  store.subscribe('currentUser', render);
  store.subscribe('notifications', render);
  // `set({ currentUser, authResolved })` emits `currentUser` first, so a render
  // triggered by the user is still looking at an unresolved session and draws
  // nothing. Without this subscription the account section would never appear
  // at all on a full page load.
  store.subscribe('authResolved', render);
  nav.append(inner);
  return nav;
}

function buildFab() {
  // A button, not a link to "#composer": there is no element with that id any
  // more, so the tap went nowhere and did nothing on any page but the feed.
  return el('button', {
    class: 'fab',
    type: 'button',
    'aria-label': t('Написать публикацию'),
    title: t('Написать публикацию'),
    onclick: goCompose,
  }, icon('plus', { size: 24 }));
}

/* -------------------------------------------------------------------------- */
/* Active link state                                                           */
/* -------------------------------------------------------------------------- */

/** Highlight the current route in every nav without a re-render. */
function wireActiveState(root) {
  const update = () => {
    const path = location.pathname.replace(/\/$/, '') || '/';
    for (const link of root.querySelectorAll('[data-nav]')) {
      const target = link.dataset.nav;
      const active = target === path
        || (target === '/' && path === '/')
        || (target === 'profile' && path.startsWith('/u/'))
        || (target === '/admin' && path.startsWith('/admin'))
        || (target !== '/' && target !== 'profile' && path.startsWith(target));
      if (active) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
  };

  update();
  // Re-evaluate on every completed navigation.
  const observer = new MutationObserver(update);
  observer.observe(document.getElementById('app'), { childList: true, subtree: true });
  window.addEventListener('popstate', update);
}
