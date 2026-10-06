/**
 * Profile page: header, tabbed content (posts, replies, media), follow action.
 */

import { compactNumber, relativeTime, absoluteTime, locale } from '../core/format.js';
import { t } from '../core/i18n.js';

import api from '../core/api.js';
import { el, clear } from '../core/dom.js';
import { icon } from '../core/icons.js';
import store, { patchCurrentUser } from '../core/store.js';
import toast from '../core/toast.js';
import { router } from '../core/router.js';
import { avatar, button, setLoading, statGrid, emptyState, errorState, skeletonRows, loadingRow, menu, alert } from '../components/ui.js';

/**
 * Open a direct conversation with somebody, creating it if needed.
 *
 * `POST /conversations` is idempotent - it returns the existing thread rather
 * than a duplicate - so this is safe to press twice, and it is the only call
 * site in the frontend. Without it there is no way to start a conversation at
 * all: the messages page can only list and open threads that already exist.
 */
async function startConversation(userPublicId, trigger) {
  if (!store.get('currentUser')) {
    router.navigate('/login');
    return;
  }
  setLoading(trigger, true);
  try {
    const data = await api.post('/conversations', { user_id: userPublicId });
    router.navigate(`/chat/${data.conversation.id}`);
  } catch (error) {
    toast.error(error.message);
  } finally {
    setLoading(trigger, false);
  }
}
import { reportDialog, confirmDialog } from '../components/report.js';

export async function render({ username }) {
  const currentUser = store.get('currentUser');
  const shell = el('div', { class: 'layout-center', style: { margin: '0 auto' } });
  shell.append(skeletonRows(2));

  let profile;
  let stats;
  let relationship = null;
  try {
    const result = await api.get(`/users/by-username/${encodeURIComponent(username)}`);
    profile = result.user;
    stats = result.stats;
    relationship = result.relationship;
  } catch (error) {
    clear(shell);
    shell.append(
      emptyState({
        iconName: 'user',
        title: error.status === 404 ? t('Профиль не найден') : t('Не удалось открыть профиль'),
        text: error.status === 404
          ? t('Возможно, профиль закрыт или имя изменено.')
          : error.message,
        action: button(t('Вернуться в ленту'), { variant: 'secondary', onClick: () => router.navigate('/') }),
      }),
    );
    return { node: shell };
  }

  document.title = t('{v0} · Гармония', { v0: profile.display_name || profile.username });
  clear(shell);

  const isSelf = currentUser && currentUser.public_id === profile.public_id;
  const canModerate = currentUser && (currentUser.role === 'moderator' || currentUser.role === 'admin');

  const panel = el('div', { class: 'feed' });
  const content = el('div', { class: 'feed', id: 'profile-content' });
  panel.append(buildHeader(profile, stats, relationship, { isSelf, canModerate, currentUser }));
  shell.append(panel, el('div', { style: { marginTop: 'var(--space-4)' } }, content));

  loadTab();

  /* ---------------------------------------------------------------------- */

  let activeTab = 'posts';
  const host = el('div', { class: 'tabs', role: 'tablist' });
  const tabs = [
    { value: 'posts', label: t('Публикации') },
    { value: 'comments', label: t('Ответы') },
    { value: 'about', label: t('О себе') },
  ];

  for (const tab of tabs) {
    host.append(
      el('button', {
        class: 'tab',
        type: 'button',
        role: 'tab',
        'aria-selected': tab.value === activeTab ? 'true' : 'false',
        onclick: (event) => {
          activeTab = tab.value;
          for (const node of host.children) node.setAttribute('aria-selected', 'false');
          event.currentTarget.setAttribute('aria-selected', 'true');
          loadTab();
        },
      }, tab.label),
    );
  }
  content.append(host, el('div', { id: 'tab-body' }));

  function loadTab() {
    const body = document.getElementById('tab-body');
    if (!body) return;
    clear(body);
    body.append(loadingRow());

    const loaders = { posts: loadPosts, comments: loadComments, about: loadAbout };
    loaders[activeTab](body);
  }

  async function loadPosts(body) {
    const { renderPost } = await import('./feed.js');
    let cursor = null;
    let done = false;

    const fetchPage = async () => {
      const params = new URLSearchParams();
      if (cursor) params.set('cursor', cursor);
      const posts = await api.get(`/users/${profile.public_id}/posts?${params}`);
      body.querySelector('.loading-row')?.remove();
      body.querySelector('.empty')?.remove();

      if (!posts.length && !cursor) {
        body.append(
          emptyState({
            iconName: 'file',
            title: isSelf ? t('Вы ещё ничего не опубликовали') : t('Пока нет публикаций'),
            text: isSelf ? t('Первая запись — самая простая.') : null,
            // The "write" action belongs on somebody *else's* profile - it is the only
            // way to start a conversation. It used to be the other way round:
            // shown on your own profile, where it navigated to the feed, and
            // absent everywhere else, so there was no route into a new
            // conversation at all.
            action: isSelf
              ? button(t('Написать пост'), { variant: 'primary', size: 'sm', onClick: () => router.navigate('/?compose=1') })
              : null,
          }),
        );
        return;
      }
      for (const post of posts) body.append(renderPost(post, body, currentUser));
      cursor = posts.__meta?.next_cursor || null;
      done = !posts.__meta?.has_more;
      if (done) {
        body.append(el('p', { class: 'text-center text-muted text-sm', style: { padding: 'var(--space-5)' }, text: t('Все публикации загружены') }));
      } else {
        body.append(
          el('div', { class: 'text-center', style: { padding: 'var(--space-3)' } },
            button(t('Показать ещё'), { variant: 'ghost', size: 'sm', onClick: (event) => { setLoading(event.currentTarget, true); fetchPage(); } }),
          ),
        );
      }
    };

    try {
      await fetchPage();
    } catch (error) {
      clear(body);
      body.append(errorState({ text: error.message, onRetry: loadTab }));
    }
  }

  async function loadComments(body) {
    try {
      const result = await api.get(`/users/${profile.public_id}/comments`);
      const comments = result.comments || [];
      body.querySelector('.loading-row')?.remove();
      if (!comments.length) {
        body.append(emptyState({ iconName: 'comment', title: t('Пока нет ответов') }));
        return;
      }
      for (const comment of comments) {
        body.append(
          el('div', { class: 'list-row' },
            icon('comment', { size: 16, className: 'text-muted' }),
            el('div', { style: { minWidth: '0', flex: '1' } },
              el('p', { class: 'clamp-2', style: { fontSize: 'var(--text-sm)', color: 'var(--text-secondary)' }, text: comment.body }),
              el('p', { class: 'text-xs text-muted', title: absoluteTime(comment.created_at) }, relativeTime(comment.created_at)),
            ),
          ),
        );
      }
    } catch (error) {
      clear(body);
      body.append(errorState({ text: error.message, onRetry: loadTab }));
    }
  }

  async function loadAbout(body) {
    body.querySelector('.loading-row')?.remove();
    const rows = [];
    if (profile.location) rows.push({ icon: 'location', label: profile.location });
    if (profile.website) rows.push({ icon: 'link', label: profile.website, href: profile.website });
    if (profile.created_at) rows.push({
      icon: 'calendar',
      label: t('в Гармонии с {v0}', {
        v0: new Date(profile.created_at).toLocaleDateString(locale(), { month: 'long', year: 'numeric' }),
      }),
    });
    if (profile.last_seen_at) rows.push({ icon: 'clock', label: t('Был(а) {v0}', { v0: relativeTime(profile.last_seen_at) }) });

    body.append(
      el('div', { class: 'card-pad' },
        profile.bio
          ? el('p', { style: { whiteSpace: 'pre-wrap', lineHeight: 'var(--leading-relaxed)', marginBottom: 'var(--space-5)' }, text: profile.bio })
          : el('p', { class: 'text-muted', style: { marginBottom: 'var(--space-5)' }, text: t('Описание не заполнено.') }),
        el('div', { class: 'profile-meta', style: { marginTop: 0 } },
          ...rows.map((row) =>
            el('span', {},
              icon(row.icon, { size: 15 }),
              row.href ? el('a', { href: row.href, rel: 'noopener noreferrer nofollow', target: '_blank', text: row.label }) : el('span', { text: row.label }),
            ),
          ),
        ),
        el('div', { style: { marginTop: 'var(--space-5)' } },
          statGrid([
            { label: t('Публикации'), value: stats?.posts_count ?? 0 },
            { label: t('Ответы'), value: stats?.comments_count ?? 0 },
            { label: t('Получили отметку'), value: stats?.likes_received ?? 0 },
          ]),
        ),
        el('div', { style: { marginTop: 'var(--space-5)' } },
          el('a', { class: 'btn btn-secondary btn-sm', href: `/api/v1/users/${profile.public_id}/data`, target: '_blank', rel: 'noopener', text: t('Данные в формате JSON') }),
        ),
      ),
    );
  }

  return { node: shell };
}

/* -------------------------------------------------------------------------- */
/* Header                                                                      */
/* -------------------------------------------------------------------------- */

function buildHeader(profile, stats, relationship, { isSelf, canModerate, currentUser }) {
  const header = el('div', { class: 'profile-header' });
  const actions = el('div', { class: 'profile-actions' });

  if (isSelf) {
    actions.append(button(t('Изменить профиль'), { variant: 'secondary', size: 'sm', onClick: () => router.navigate('/settings/profile') }));
  } else if (currentUser) {
    // Writing a message is the point of visiting somebody else's profile. It
    // belongs next to "follow", and it is the only entry point into a new
    // conversation in the whole interface.
    actions.append(
      button(t('Написать сообщение'), {
        variant: 'primary',
        size: 'sm',
        onClick: (event) => startConversation(profile.public_id, event.currentTarget),
      }),
    );
    actions.append(followButton(profile, relationship));
    if (canModerate) {
      actions.append(
        menu([
          { label: t('Пожаловаться'), icon: 'flag', onClick: () => reportDialog({ targetType: 'user', targetId: profile.public_id }) },
          { label: t('Панель модератора'), icon: 'shield', onClick: () => router.navigate(`/admin/users?q=${encodeURIComponent(profile.username)}`) },
        ]),
      );
    } else {
      actions.append(
        el('button', {
          class: 'icon-btn',
          type: 'button',
          'aria-label': t('Пожаловаться'),
          title: t('Пожаловаться'),
          onclick: () => reportDialog({ targetType: 'user', targetId: profile.public_id }),
        }, icon('flag', { size: 18 })),
      );
    }
  } else {
    actions.append(button(t('Войти'), { variant: 'primary', size: 'sm', onClick: () => router.navigate('/login') }));
  }

  const meta = el('div', { class: 'profile-meta' });
  if (profile.location) meta.append(el('span', {}, icon('location', { size: 15 }), profile.location));
  if (profile.website) {
    meta.append(el('span', {}, icon('link', { size: 15 }),
      el('a', { href: profile.website, rel: 'noopener noreferrer nofollow', target: '_blank', text: profile.website.replace(/^https?:\/\//, '') }),
    ));
  }
  meta.append(el('span', {}, icon('calendar', { size: 15 }),
      t('в Гармонии с {v0}', {
        v0: new Date(profile.created_at).toLocaleDateString(locale(), { month: 'long', year: 'numeric' }),
      }),
  ));
  if (profile.last_seen_at) meta.append(el('span', {}, icon('clock', { size: 15 }), t('Был(а) {v0}', { v0: relativeTime(profile.last_seen_at) })));

  header.append(
    el('div', { class: 'profile-top' },
      avatar(profile, { size: '2xl', link: false }),
      el('div', { class: 'profile-identity' },
        el('h1', { class: 'profile-name' },
          profile.display_name || profile.username,
          profile.is_verified ? icon('verified', { size: 17, title: t('Подтверждённый аккаунт') }) : null,
        ),
        el('div', { class: 'profile-handle', text: `@${profile.username}` }),
        profile.pronouns ? el('div', { class: 'text-sm text-muted', text: profile.pronouns }) : null,
      ),
      actions,
    ),
    profile.bio ? el('p', { class: 'profile-bio', text: profile.bio }) : null,
    meta,
    el('div', { class: 'profile-stats', style: { marginTop: 'var(--space-3)' } },
      statLink(stats?.posts_count, t('публикаций'), `#posts`),
      statLink(stats?.followers_count, relationship?.follows_you ? t('подписчиков · подписан(а)') : t('подписчиков'), '#followers'),
      statLink(stats?.following_count, t('подписок'), '#following'),
    ),
  );

  return header;
}

function statLink(value, label, href) {
  return el('a', { class: 'profile-stat', href, style: { color: 'inherit' } },
    el('span', { class: 'value', text: compactNumber(value || 0) }),
    el('span', { class: 'label', text: label }),
  );
}

function followButton(profile, relationship) {
  const following = Boolean(relationship?.is_following);
  const node = button(following ? t('Вы подписаны') : t('Подписаться'), {
    variant: following ? 'secondary' : 'primary',
    size: 'sm',
  });

  node.addEventListener('click', async () => {
    setLoading(node, true);
    try {
      if (following) {
        await api.delete(`/users/${profile.public_id}/follow`);
        node.textContent = t('Подписаться');
        node.className = 'btn btn-primary btn-sm';
        toast.info(t('Вы отписались от @{v0}', { v0: profile.username }));
      } else {
        await api.post(`/users/${profile.public_id}/follow`);
        node.textContent = t('Вы подписаны');
        node.className = 'btn btn-secondary btn-sm';
        toast.success(t('Вы подписались на @{v0}', { v0: profile.username }));
      }
    } catch (error) {
      toast.error(error.message);
    } finally {
      setLoading(node, false);
    }
  });

  return node;
}
