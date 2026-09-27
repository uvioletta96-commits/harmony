/**
 * Search page: people and posts, with debounced input and URL-synced state.
 *
 * The query lives in the URL so a search can be shared or bookmarked, and so
 * the back button behaves the way people expect.
 */

import { t } from '../core/i18n.js';

import api from '../core/api.js';
import { el, clear, $ } from '../core/dom.js';
import { icon } from '../core/icons.js';
import store from '../core/store.js';
import { router } from '../core/router.js';
import { personRow, button, emptyState, errorState, skeletonRows, avatar } from '../components/ui.js';
import { renderPost } from './feed.js';

const DEBOUNCE_MS = 280;

export async function render() {
  const currentUser = store.get('currentUser');
  const initial = new URLSearchParams(location.search).get('q') || '';

  const input = el('input', {
    class: 'input',
    type: 'search',
    name: 'q',
    value: initial,
    placeholder: t('Имена пользователей, текст публикаций, #хэштеги'),
    'aria-label': t('Поисковый запрос'),
    autocomplete: 'off',
  });

  const results = el('div', { class: 'feed' });
  const tabsHost = el('div', { class: 'feed-tabs', role: 'tablist' });
  const panel = el('div', { style: { marginTop: 'var(--space-4)' } }, results);
  const shell = el('div', { class: 'layout-center', style: { margin: '0 auto' } },
    el('div', { class: 'feed' },
      el('div', { class: 'search-head' },
        el('div', { class: 'search-input-wrap' },
          icon('search', { class: 'icon' }),
          input,
        ),
      ),
      tabsHost,
    ),
    panel,
  );

  let mode = new URLSearchParams(location.search).get('type') || 'all';
  let requestId = 0;

  const setTabs = () => {
    clear(tabsHost);
    for (const [value, label] of [['all', t('Всё')], ['people', t('Люди')], ['posts', t('Публикации')]]) {
      tabsHost.append(
        el('button', {
          class: 'tab',
          type: 'button',
          role: 'tab',
          'aria-selected': value === mode ? 'true' : 'false',
          onclick: () => {
            mode = value;
            setTabs();
            run();
          },
        }, label),
      );
    }
  };

  const run = async () => {
    const query = input.value.trim();
    const url = new URL(location.href);
    if (query) url.searchParams.set('q', query);
    else url.searchParams.delete('q');
    if (mode !== 'all') url.searchParams.set('type', mode);
    else url.searchParams.delete('type');
    history.replaceState(null, '', url);

    if (query.length < 2) {
      clear(results);
      results.append(
        emptyState({
          iconName: 'search',
          title: query ? t('Введите ещё символ') : t('Что найти?'),
          text: query ? t('Минимум два символа.') : t('Поищите по имени пользователя, фразе из публикации или хэштегу.'),
        }),
      );
      return;
    }

    const ticket = ++requestId;
    clear(results);
    results.append(skeletonRows(2));

    try {
      if (mode === 'people') {
        const people = await api.get(`/users/search?q=${encodeURIComponent(query)}`);
        if (ticket !== requestId) return;
        renderPeople(people);
      } else if (mode === 'posts') {
        const posts = await api.get(`/posts/search?q=${encodeURIComponent(query)}`);
        if (ticket !== requestId) return;
        renderPosts(posts);
      } else {
        const [people, posts] = await Promise.all([
          api.get(`/users/search?q=${encodeURIComponent(query)}`),
          api.get(`/posts/search?q=${encodeURIComponent(query)}`),
        ]);
        if (ticket !== requestId) return;
        renderMixed(people, posts);
      }
    } catch (error) {
      if (ticket !== requestId) return;
      clear(results);
      results.append(errorState({ text: error.message, onRetry: run }));
    }
  };

  const renderPeople = (people) => {
    clear(results);
    if (!people.length) {
      results.append(emptyState({ iconName: 'user', title: t('Никого не нашли'), text: t('Попробуйте другое написание.') }));
      return;
    }
    for (const person of people) {
      results.append(
        personRow(person, {
          action: currentUser && person.public_id !== currentUser.public_id
            ? button(t('Подписаться'), { variant: 'secondary', size: 'sm', onClick: (event) => follow(event, person) })
            : null,
        }),
      );
    }
  };

  const renderPosts = (posts) => {
    clear(results);
    if (!posts.length) {
      results.append(emptyState({ iconName: 'file', title: t('Публикаций не найдено'), text: t('Попробуйте другие слова или #хэштег.') }));
      return;
    }
    for (const post of posts) results.append(renderPost(post, results, currentUser));
  };

  const renderMixed = (people, posts) => {
    clear(results);
    if (!people.length && !posts.length) {
      results.append(emptyState({ iconName: 'search', title: t('Ничего не найдено'), text: t('Проверьте раскладку клавиатуры и написание.') }));
      return;
    }
    if (people.length) {
      results.append(el('div', { class: 'rail-head', style: { background: 'var(--bg-sunken)' } }, t('Люди')));
      for (const person of people.slice(0, 5)) {
        results.append(
          personRow(person, {
            action: currentUser && person.public_id !== currentUser.public_id
              ? button(t('Подписаться'), { variant: 'secondary', size: 'sm', onClick: (event) => follow(event, person) })
              : null,
          }),
        );
      }
    }
    if (posts.length) {
      results.append(el('div', { class: 'rail-head', style: { background: 'var(--bg-sunken)' } }, t('Публикации')));
      for (const post of posts.slice(0, 20)) results.append(renderPost(post, results, currentUser));
    }
  };

  async function follow(event, person) {
    const { setLoading } = await import('../components/ui.js');
    const node = event.currentTarget;
    setLoading(node, true);
    try {
      await api.post(`/users/${person.public_id}/follow`);
      node.textContent = t('Вы подписаны');
      node.className = 'btn btn-secondary btn-sm';
      setLoading(node, false);
    } catch (error) {
      setLoading(node, false);
      const { default: toast } = await import('../core/toast.js');
      toast.error(error.message);
    }
  }

  let timer = null;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(run, DEBOUNCE_MS);
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      clearTimeout(timer);
      run();
    }
    if (event.key === 'Escape') {
      input.value = '';
      run();
    }
  });

  setTabs();
  if (initial) run();
  else {
    results.append(
      emptyState({
        iconName: 'search',
        title: t('Что найти?'),
        text: t('Поищите по имени пользователя, фразе из публикации или хэштегу.'),
      }),
    );
  }

  if (initial) requestAnimationFrame(() => input.focus());
  return { node: shell };
}
