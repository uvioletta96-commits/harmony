/**
 * Client-side router.
 *
 * History-API routing with a tiny view registry. A route is registered with a
 * `render(params)` function that returns one of:
 *
 *   - a DOM node — the common case;
 *   - `{ node, unmount? }` — when the page has something to tear down;
 *   - `{ node, wide: true }` - take the right rail's column as well, for a
 *     form or table the narrow middle column cannot hold.
 *   - `{ redirect }` — decline to render and send the user elsewhere.
 *
 * Teardown is part of the contract rather than an afterthought. A listener or
 * timer left behind after navigation is the most common source of "it works
 * until you visit another page" bugs, so every page that starts anything is
 * expected to return the function that stops it.
 */

import { t } from '../core/i18n.js';

import { $, clear } from './dom.js';

const routes = [];
let currentCleanup = null;
let notFoundView = null;
let outlet = null;
let beforeEachGuard = null;

export function define(pattern, loader) {
  // "/u/:username" -> /^\/u\/([^/]+)$/
  const names = [];
  const regexSource = pattern
    .replace(/\/$/, '')
    .replace(/[.+*?^${}()|[\]\\]/g, '\\$&')
    .replace(/:(\w+)/g, (_match, name) => {
      names.push(name);
      return '([^/]+)';
    });
  routes.push({ regex: new RegExp(`^${regexSource || '/'}/?$`), names, loader, pattern });
}

export function setNotFoundView(loader) {
  notFoundView = loader;
}

export function setGuard(guard) {
  beforeEachGuard = guard;
}

export function navigate(path, { replace = false, state: historyState = null } = {}) {
  if (replace) {
    history.replaceState(historyState, '', path);
  } else {
    history.pushState(historyState, '', path);
  }
  return resolve();
}

export function currentPath() {
  return location.pathname + location.search;
}

function match(path) {
  const clean = path.split('?')[0].replace(/\/$/, '') || '/';
  for (const route of routes) {
    const result = route.regex.exec(clean);
    if (result) {
      const params = {};
      route.names.forEach((name, index) => {
        params[name] = decodeURIComponent(result[index + 1]);
      });
      return { route, params };
    }
  }
  return null;
}

export async function resolve() {
  const path = currentPath();

  if (beforeEachGuard) {
    const verdict = await beforeEachGuard(path);
    if (verdict === false) return;
    if (typeof verdict === 'string') {
      return navigate(verdict, { replace: true });
    }
  }

  if (currentCleanup) {
    try {
      await currentCleanup();
    } catch (error) {
      console.error('[router] unmount failed', error);
    }
    currentCleanup = null;
  }

  const matched = match(path);
  const target = matched ? matched.route : null;
  const loader = target ? target.loader : notFoundView;

  if (!loader) {
    console.warn(`[router] no view registered for ${path}`);
    return;
  }

  document.title = defaultTitle();

  try {
    const render = typeof loader === 'function' ? loader : loader.render;
    // ``main`` first: it is the layout's centre column and the landmark the
    // page belongs inside. ``#app`` is the fallback for a document with no
    // shell - a bare test page, say - where there is no main to write into.
    outlet = outlet || $('main#main') || $('#app');
    if (!outlet) {
      console.error('[router] no #app outlet in the document');
      return;
    }

    // A page may decline to render and hand navigation back to the router:
    // `{ redirect: '/login' }`. This is the only supported way to redirect from
    // inside a render function. Calling router.navigate() and then continuing
    // to build a view races the two: the outgoing view gets appended after the
    // incoming one, and the user is left staring at the page they just left,
    // needing a refresh to see where they actually went.
    const view = await render(matched ? matched.params : {});
    if (view && view.redirect) {
      return navigate(view.redirect, { replace: true });
    }

    clear(outlet);
    // A page may return the node directly for the common case, or
    // `{ node, unmount }` when it has something to tear down.
    const node = view && view.node ? view.node : view;
    if (!(node instanceof Node)) {
      throw new TypeError(`[router] ${matched ? matched.route.pattern : 'not-found'} render() returned no node`);
    }
    outlet.append(node);
    applyWidth(view);
    currentCleanup = (view && view.unmount) || null;
    window.scrollTo({ top: 0, behavior: 'instant' });
  } catch (error) {
    console.error('[router] view failed', error);
    if (outlet) {
      clear(outlet);
      const div = document.createElement('div');
      div.className = 'empty';
      div.innerHTML =
        t('<div class="empty-title">Не удалось открыть страницу</div>') +
        t('<div class="empty-text">Попробуйте обновить страницу или вернуться позже.</div>');
      outlet.append(div);
    }
  }
}

/**
 * Let a page claim the right rail's column.
 *
 * The shell is a three-column grid: navigation, content, discovery. Content
 * gets the middle column only, which is right for a feed and badly wrong for a
 * settings form - 456px minus a 208px section rail left 224px for every label
 * and input, so fields wrapped mid-word and the whole page read as squeezed.
 *
 * `{ node, wide: true }` is the opt-in. The rail is hidden rather than merely
 * overflowed, because rail content is discovery and someone filling in a
 * settings form is not discovering anything.
 *
 * The class is set on every render, not only when `wide` is set: a page that
 * opted out has to be able to opt back in, and a stale class left behind is how
 * a page that works in isolation breaks when you reach it from somewhere else.
 */
function applyWidth(view) {
  const shell = outlet && outlet.closest ? outlet.closest('.layout') : null;
  if (shell) shell.classList.toggle('is-wide', Boolean(view && view.wide));
}

function defaultTitle() {
  return t('Гармония');
}

export function startRouter() {
  window.addEventListener('popstate', () => resolve());

  // Intercept same-origin link clicks so navigation stays client-side.
  document.addEventListener('click', (event) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    const link = event.target.closest('a[href]');
    if (!link) return;
    if (link.target === '_blank' || link.hasAttribute('download') || link.dataset.external === 'true') return;

    const href = link.getAttribute('href');
    if (!href || href.startsWith('http') || href.startsWith('//') || href.startsWith('mailto:') || href.startsWith('#')) return;

    event.preventDefault();
    navigate(href);
  });

  return resolve();
}

export const router = {
  define,
  navigate,
  resolve,
  start: startRouter,
  currentPath,
  setNotFoundView,
  setGuard,
};

export default router;
