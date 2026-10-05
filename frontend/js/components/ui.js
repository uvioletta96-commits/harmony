/**
 * Reusable UI components.
 *
 * Each returns a DOM node plus, where relevant, a controller object. No
 * component reaches into another component's internals, and none of them
 * insert user-supplied text as markup.
 */

import { t } from '../core/i18n.js';

import { el, clear, delegate, toggleClass } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { relativeTime, absoluteTime, compactNumber, colourFor, initialsOf, plural } from '../core/format.js';

/* -------------------------------------------------------------------------- */
/* Avatar                                                                      */
/* -------------------------------------------------------------------------- */

export function avatar(user, { size = 'md', link = true, showOnline = false } = {}) {
  if (!user) return el('div', { class: 'hidden' });

  const name = user.display_name || user.username || '?';
  const node = el('div', {
    class: `avatar avatar-${size}`,
    dataset: { color: user.avatar_color || colourFor(user.username || name) },
    role: 'img',
    'aria-label': name,
  });

  if (user.avatar_url) {
    const img = el('img', { src: user.avatar_url, alt: '', loading: 'lazy', decoding: 'async' });
    // A broken avatar URL must not leave a broken-image icon in place of a face.
    img.addEventListener('error', () => {
      img.remove();
      node.textContent = initialsOf(name);
    });
    node.append(img);
  } else {
    node.append(el('span', { text: user.initials || initialsOf(name) }));
  }

  if (link && user.username) {
    const anchor = el('a', { href: `/u/${encodeURIComponent(user.username)}`, 'aria-label': name, tabindex: '-1' }, node);
    return showOnline ? wrapWithPresence(anchor, user) : anchor;
  }
  return showOnline ? wrapWithPresence(node, user) : node;
}

function wrapWithPresence(node, user) {
  return el('div', { class: 'avatar-wrap' }, node, el('span', { class: 'avatar-dot', 'aria-hidden': 'true' }));
}

/* -------------------------------------------------------------------------- */
/* Buttons                                                                     */
/* -------------------------------------------------------------------------- */

export function button(label, { variant = 'secondary', size = '', iconName = null, onClick, type = 'button', disabled = false, title = null, ariaLabel = null } = {}) {
  const classes = ['btn', `btn-${variant}`];
  if (size) classes.push(`btn-${size}`);
  const node = el(
    'button',
    {
      class: classes,
      type,
      disabled,
      title: title || undefined,
      'aria-label': ariaLabel || (iconName && !label ? title : undefined),
      onclick: onClick,
    },
    iconName ? icon(iconName) : null,
    label ? el('span', { text: label }) : null,
  );
  return node;
}

export function iconButton(iconName, { onClick, title, badge = 0, variant = 'ghost', href = null } = {}) {
  const node = el(
    href ? 'a' : 'button',
    {
      class: `icon-btn btn-${variant}`,
      href: href || undefined,
      type: href ? undefined : 'button',
      title,
      'aria-label': title,
      onclick: onClick,
    },
    icon(iconName, { size: 19 }),
    badge > 0 ? el('span', { class: 'badge-count', text: String(badge > 99 ? '99+' : badge) }) : null,
  );
  return node;
}

export function setLoading(node, loading) {
  if (!node) return;
  toggleClass(node, 'is-loading', loading);
  node.disabled = loading;
}

/* -------------------------------------------------------------------------- */
/* Inputs                                                                      */
/* -------------------------------------------------------------------------- */

export function field({ name, label, type = 'text', value = '', placeholder = '', required = false, hint = '', autocomplete = null, maxLength = null, optional = false, inputMode = null }) {
  const input = el('input', {
    class: 'input',
    type,
    name,
    value: value ?? '',
    placeholder,
    required,
    autocomplete,
    maxlength: maxLength || undefined,
    inputmode: inputMode || undefined,
    'aria-required': required ? 'true' : 'false',
  });

  const labelNode = el('label', { class: 'label', for: `f-${name}` },
    label,
    optional ? el('span', { class: 'optional', text: 'необязательно' }) : null,
  );
  input.id = `f-${name}`;

  return el('div', { class: 'field' },
    labelNode,
    input,
    hint ? el('p', { class: 'hint', text: hint }) : null,
    el('p', { class: 'error-text hidden', role: 'alert' }),
  );
}

export function textareaField({ name, label, value = '', placeholder = '', rows = 4, maxLength = null, hint = '', required = false }) {
  const area = el('textarea', {
    class: 'textarea',
    name,
    rows: String(rows),
    placeholder,
    required,
    maxlength: maxLength || undefined,
    id: `f-${name}`,
  });
  area.value = value ?? '';

  return el('div', { class: 'field' },
    el('label', { class: 'label', for: `f-${name}` }, label),
    area,
    hint ? el('p', { class: 'hint', text: hint }) : null,
    el('p', { class: 'error-text hidden', role: 'alert' }),
  );
}

export function checkbox({ name, label, checked = false, hint = '' }) {
  const input = el('input', { type: 'checkbox', name, id: `f-${name}` });
  input.checked = checked;
  return el('label', { class: 'checkbox', for: `f-${name}` },
    input,
    el('span', {},
      el('span', { text: label }),
      hint ? el('span', { class: 'hint', style: { display: 'block' }, text: hint }) : null,
    ),
  );
}

export function toggle({ name, label, hint = '', checked = false, disabled = false }) {
  const input = el('input', { type: 'checkbox', name, id: `f-${name}`, disabled });
  input.checked = checked;
  return el('label', { class: 'switch', for: `f-${name}` },
    input,
    el('span', { class: 'track' }),
    el('span', { class: 'switch-text' },
      el('span', { class: 'switch-label', text: label }),
      hint ? el('span', { class: 'switch-hint', text: hint }) : null,
    ),
  );
}

export function select({ name, label, options, value = '', hint = '', required = false }) {
  const node = el('select', { class: 'select', name, id: `f-${name}`, required, 'aria-required': required ? 'true' : 'false' });
  for (const option of options) {
    const opt = el('option', { value: option.value }, option.label);
    if (option.value === value) opt.selected = true;
    node.append(opt);
  }
  return el('div', { class: 'field' },
    el('label', { class: 'label', for: `f-${name}` }, label),
    node,
    hint ? el('p', { class: 'hint', text: hint }) : null,
    el('p', { class: 'error-text hidden', role: 'alert' }),
  );
}

/* -------------------------------------------------------------------------- */
/* Post                                                                        */
/* -------------------------------------------------------------------------- */

/**
 * Render a post.
 * Content arrives either as raw text (rendered as text — safe by construction)
 * or as typed spans from the server, which this function also renders as text
 * nodes with an appropriate class. No path in this function produces markup.
 */
export function postCard(
  post,
  { currentUser, onLike, onComment, onEdit, onDelete, onReport, onShare, onProfile } = {},
) {
  const author = post.author || {};
  const isOwner = currentUser && author.public_id === currentUser.public_id;

  const time = relativeTime(post.created_at);
  const timeNode = el('time', { datetime: post.created_at || '', title: absoluteTime(post.created_at) }, time);

  const head = el('div', { class: 'post-head' },
    el('a', { class: 'post-author', href: `/u/${encodeURIComponent(author.username || '')}`, onclick: onProfile }, author.display_name || author.username || 'Аккаунт удалён'),
    author.username ? el('span', { class: 'post-handle', text: `@${author.username}` }) : null,
  );

  const meta = el('div', { class: 'post-meta' },
    timeNode,
    el('span', { class: 'dot' }),
    el('span', { text: visibilityLabel(post.visibility) }),
    post.edited_at ? el('span', { class: 'dot' }, el('span', { text: 'изменено' })) : null,
  );

  const body = el('div', { class: 'post-text' });
  if (post.spans?.length) {
    for (const span of post.spans) appendSpan(body, span);
  } else {
    body.textContent = post.body || '';
  }

  const media = renderMedia(post.media || []);

  const likeNode = el('button', {
    class: `post-action ${post.viewer_has_liked ? 'is-active' : ''}`,
    type: 'button',
    'aria-pressed': post.viewer_has_liked ? 'true' : 'false',
    'aria-label': post.viewer_has_liked ? 'Убрать отметку «Нравится»' : 'Отметить «Нравится»',
    onclick: (event) => onLike?.(event, post),
  },
    icon('heart'),
    el('span', { class: 'count', text: post.likes_count > 0 ? compactNumber(post.likes_count) : '' }),
  );

  const commentNode = el('button', {
    class: 'post-action',
    type: 'button',
    'aria-label': 'Комментарии',
    onclick: () => onComment?.(post),
  },
    icon('comment'),
    el('span', { class: 'count', text: post.comments_count > 0 ? compactNumber(post.comments_count) : '' }),
  );

  const shareNode = el('button', {
    class: 'post-action',
    type: 'button',
    'aria-label': 'Поделиться',
    onclick: () => onShare?.(post),
  }, icon('share'));

  const actions = el('div', { class: 'post-actions' }, likeNode, commentNode, shareNode);

  if (isOwner || currentUser?.role === 'moderator' || currentUser?.role === 'admin') {
    actions.append(menu([
      isOwner ? { label: 'Редактировать', icon: 'edit', onClick: () => onEdit?.(post) } : null,
      { label: 'Удалить', icon: 'trash', danger: true, onClick: () => onDelete?.(post) },
    ].filter(Boolean)));
  } else if (currentUser) {
    actions.append(
      el('button', {
        class: 'post-action',
        type: 'button',
        'aria-label': 'Пожаловаться',
        onclick: () => onReport?.(post),
      }, icon('flag')),
    );
  }

  const card = el('article', { class: 'post', dataset: { postId: post.id } },
    avatar(author, { showOnline: true, onProfile }),
    el('div', { class: 'post-body' }, head, meta, body, media, actions),
  );

  if (post.status === 'under_review') {
    card.append(el('div', { class: 'alert alert-warning', style: { margin: '0 1.25rem 1rem', gridColumn: '1 / -1' } },
      icon('clock', { class: 'icon' }),
      el('div', { class: 'alert-body' }, 'Публикация ожидает проверки модератора.'),
    ));
  }

  return card;
}

function appendSpan(container, span) {
  if (span.type === 'link') {
    container.append(el('a', { href: span.url, rel: 'noopener noreferrer nofollow', target: '_blank' }, span.text));
  } else if (span.type === 'mention') {
    container.append(el('a', { class: 'mention', href: `/u/${encodeURIComponent(span.username)}` }, span.text));
  } else if (span.type === 'hashtag') {
    container.append(el('a', { class: 'hashtag', href: `/search?q=${encodeURIComponent(span.tag)}` }, span.text));
  } else {
    container.append(document.createTextNode(span.text));
  }
}

function visibilityLabel(visibility) {
  return { public: 'Публично', followers: 'Подписчикам', private: 'Только мне' }[visibility] || '';
}

/**
 * Build the node for one attachment.
 *
 * The branch is on `mime_type` rather than on the file extension in the URL,
 * because the URL suffix is chosen from the sniffed container and the
 * `mime_type` field is what the server actually verified. A `<video>` for a
 * still image shows a black rectangle with a broken player, so the two cases
 * are rendered by different code paths rather than by one element hoping the
 * browser sorts it out.
 */
function renderAttachment(item) {
  const isVideo = typeof item.mime_type === 'string' && item.mime_type.startsWith('video/');

  if (isVideo) {
    const video = el('video', {
      class: 'post-video',
      src: item.url,
      // Asked for: sound on, playing by itself.
      //
      // Browsers decide whether to allow that. Chrome and Safari block audible
      // autoplay unless the visitor has interacted with the page or the site
      // has enough engagement to qualify, and no attribute can override it -
      // `autoplay` plus `muted: false` is the request, not a guarantee. The
      // fallback below is what happens when the request is refused, so the
      // clip still plays and still has sound, one tap away.
      autoplay: '',
      muted: false,
      loop: '',
      controls: '',
      preload: 'metadata',
      playsinline: '',
      poster: item.thumbnail_url || undefined,
      'aria-label': item.alt_text || 'Видео',
    });

    // If the browser refused audible autoplay, `play()` rejects with
    // NotAllowedError. Starting muted and marking the clip means the reader
    // gets the video plus one visible control, instead of a poster that never
    // moves and no indication why.
    const startMutedFallback = () => {
      video.muted = true;
      video.classList.add('is-blocked');
      video.play().catch(() => {
        /* Even muted autoplay can be refused; the controls are already there. */
      });
    };

    const attempt = video.play();
    if (attempt && typeof attempt.catch === 'function') {
      attempt.catch(startMutedFallback);
    }

    // One video at a time. Three overlapping clips from a feed is noise, and on
    // a phone it is worse than noise: the audio tracks stack and there is no
    // way to tell which one to mute. Starting one pauses the rest, which is the
    // behaviour every video feed converges on.
    video.addEventListener('play', () => {
      for (const other of document.querySelectorAll('video.playing')) {
        if (other !== video) other.pause();
      }
      video.classList.add('playing');
    });
    const stopPlaying = () => video.classList.remove('playing');
    video.addEventListener('pause', stopPlaying);
    video.addEventListener('ended', stopPlaying);
    // A tap anywhere on a blocked clip is the gesture that unlocks sound.
    video.addEventListener('click', () => {
      if (video.classList.contains('is-blocked')) {
        video.muted = false;
        video.classList.remove('is-blocked');
        video.play().catch(() => {});
      }
    });
    return video;
  }

  const img = el('img', {
    src: item.thumbnail_url || item.url,
    alt: item.alt_text || '',
    loading: 'lazy',
    decoding: 'async',
    // No width/height means the layout shifts as images arrive.
    width: item.width ? String(item.width) : undefined,
    height: item.height ? String(item.height) : undefined,
  });
  img.addEventListener('click', () => openLightbox(item));
  return img;
}

function renderMedia(items) {
  if (!items.length) return null;
  const grid = el('div', { class: 'post-media', dataset: { count: String(Math.min(items.length, 4)) } });
  for (const item of items.slice(0, 4)) {
    grid.append(renderAttachment(item));
  }
  return grid;
}

export function openLightbox(item) {
  const overlay = el('div', { class: 'lightbox', role: 'dialog', 'aria-modal': 'true', 'aria-label': item.alt_text || 'Изображение' });
  const image = el('img', { src: item.url, alt: item.alt_text || '' });
  const close = el('button', { class: 'lightbox-close', type: 'button', 'aria-label': 'Закрыть' }, icon('close', { size: 20 }));

  const dismiss = () => {
    overlay.remove();
    document.removeEventListener('keydown', onKey);
    document.body.style.overflow = '';
  };
  const onKey = (event) => {
    if (event.key === 'Escape') dismiss();
  };

  overlay.append(image, close);
  overlay.addEventListener('click', dismiss);
  close.addEventListener('click', (event) => {
    event.stopPropagation();
    dismiss();
  });
  document.addEventListener('keydown', onKey);
  document.body.style.overflow = 'hidden';
  document.body.append(overlay);
  close.focus();
}

/* -------------------------------------------------------------------------- */
/* Comment                                                                     */
/* -------------------------------------------------------------------------- */

export function commentItem(comment, { currentUser, onReply, onDelete, onLike } = {}) {
  const author = comment.author || {};
  const isOwner = currentUser && author.public_id === currentUser.public_id;
  const deleted = comment.status !== 'published';

  const body = el('div', { class: 'comment-body', text: deleted ? 'Комментарий удалён.' : comment.body });

  const actions = el('div', { class: 'comment-actions' },
    el('span', { class: 'text-xs text-muted' }, relativeTime(comment.created_at)),
    !deleted && onReply
      ? el('button', { class: 'post-action', type: 'button', onclick: () => onReply(comment) }, icon('comment', { size: 14 }), el('span', { text: 'Ответить' }))
      : null,
    isOwner && onDelete
      ? el('button', { class: 'post-action', type: 'button', onclick: () => onDelete(comment) }, icon('trash', { size: 14 }), el('span', { text: 'Удалить' }))
      : null,
  );

  return el('div', { class: 'comment', id: `comment-${comment.id}`, dataset: { depth: String(Math.min(comment.depth || 0, 3)), isDeleted: String(deleted) } },
    avatar(author, { size: 'sm' }),
    el('div', {},
      el('div', {},
        el('a', { href: `/u/${encodeURIComponent(author.username || '')}`, style: { fontWeight: '500', fontSize: 'var(--text-sm)' } },
          author.display_name || author.username || 'Аккаунт удалён'),
        el('span', { class: 'text-xs text-muted', style: { marginLeft: '6px' }, text: `@${author.username || ''}` }),
      ),
      body,
      actions,
      comment.replies?.length
        ? el('div', {}, ...comment.replies.map((reply) => commentItem(reply, { currentUser, onReply, onDelete, onLike })))
        : null,
    ),
  );
}

/* -------------------------------------------------------------------------- */
/* Dropdown menu                                                               */
/* -------------------------------------------------------------------------- */

/**
 * Actions menu.
 * Closes on outside click, Escape, scroll and navigation — a menu that stays
 * open after the page moves under it is a classic source of confusion.
 */
export function menu(items, { align = 'right' } = {}) {
  const wrap = el('div', { class: 'dropdown' });
  const trigger = el('button', {
    class: 'post-action',
    type: 'button',
    'aria-haspopup': 'menu',
    'aria-expanded': 'false',
    'aria-label': 'Действия',
  }, icon('more', { size: 16 }));

  const list = el('div', { class: `dropdown-menu ${align === 'left' ? 'dropdown-menu-left' : ''}`, role: 'menu' });
  for (const item of items) {
    if (item === '-' || item.separator) {
      list.append(el('div', { class: 'dropdown-separator', role: 'separator' }));
      continue;
    }
    list.append(el('button', {
      class: `dropdown-item ${item.danger ? 'dropdown-item-danger' : ''}`,
      type: 'button',
      role: 'menuitem',
      onclick: () => {
        close();
        item.onClick?.();
      },
    }, item.icon ? icon(item.icon, { size: 16 }) : null, el('span', { text: item.label })));
  }

  let open = false;
  const openMenu = () => {
    if (open) return;
    open = true;
    list.style.display = 'block';
    trigger.setAttribute('aria-expanded', 'true');
    setTimeout(() => {
      document.addEventListener('click', onOutside, true);
      document.addEventListener('keydown', onKey, true);
      window.addEventListener('scroll', close, { once: true, capture: true });
    }, 0);
  };
  const close = () => {
    if (!open) return;
    open = false;
    list.style.display = 'none';
    trigger.setAttribute('aria-expanded', 'false');
    document.removeEventListener('click', onOutside, true);
    document.removeEventListener('keydown', onKey, true);
  };
  const onOutside = (event) => {
    if (!wrap.contains(event.target)) close();
  };
  const onKey = (event) => {
    if (event.key === 'Escape') {
      close();
      trigger.focus();
    }
  };

  trigger.addEventListener('click', (event) => {
    event.stopPropagation();
    open ? close() : openMenu();
  });

  list.style.display = 'none';
  wrap.append(trigger, list);
  wrap.close = close;
  return wrap;
}

/* -------------------------------------------------------------------------- */
/* States                                                                      */
/* -------------------------------------------------------------------------- */

export function emptyState({ iconName = 'sparkle', title, text, action = null }) {
  return el('div', { class: 'empty' },
    icon(iconName, { class: 'empty-icon', strokeWidth: 1.3 }),
    el('p', { class: 'empty-title', text: title }),
    text ? el('p', { class: 'empty-text', text }) : null,
    action,
  );
}

export function errorState({ title = 'Что-то пошло не так', text, onRetry }) {
  return el('div', { class: 'empty' },
    icon('alert', { class: 'empty-icon', strokeWidth: 1.3 }),
    el('p', { class: 'empty-title', text: title }),
    text ? el('p', { class: 'empty-text', text }) : null,
    onRetry ? button('Повторить', { variant: 'secondary', size: 'sm', onClick: onRetry }) : null,
  );
}

export function postSkeleton() {
  return el('div', { class: 'post-skeleton' },
    el('div', { class: 'skeleton skeleton-circle' }),
    el('div', { style: { paddingTop: '2px' } },
      el('div', { class: 'skeleton skeleton-text', style: { width: '32%' } }),
      el('div', { class: 'skeleton skeleton-text', style: { width: '92%' } }),
      el('div', { class: 'skeleton skeleton-text', style: { width: '74%' } }),
    ),
  );
}

export function skeletonRows(count = 4) {
  const fragment = document.createDocumentFragment();
  for (let index = 0; index < count; index += 1) fragment.append(postSkeleton());
  return fragment;
}

export function loadingRow(label = 'Загрузка…') {
  return el('div', { class: 'loading-row' }, el('span', { class: 'spinner' }), el('span', { text: label }));
}

export function alert({ variant = 'info', text, title = null }) {
  return el('div', { class: `alert alert-${variant}`, role: variant === 'danger' ? 'alert' : 'status' },
    icon(variant === 'danger' ? 'alert' : variant === 'warning' ? 'alert' : 'info', { class: 'icon' }),
    el('div', { class: 'alert-body' },
      title ? el('strong', { text: title }) : null,
      text ? el('div', { text }) : null,
    ),
  );
}

/* -------------------------------------------------------------------------- */
/* Modal                                                                       */
/* -------------------------------------------------------------------------- */

export function modal({ title, body, actions = [], onClose, wide = false }) {
  const previousFocus = document.activeElement;
  const dialog = el('div', { class: `modal ${wide ? 'modal-lg' : ''}`, role: 'dialog', 'aria-modal': 'true', 'aria-label': title });

  const close = () => {
    backdrop.remove();
    document.removeEventListener('keydown', onKey, true);
    document.body.style.overflow = '';
    previousFocus?.focus?.();
    onClose?.();
  };

  const onKey = (event) => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      close();
    }
    if (event.key === 'Tab') trapFocus(event, dialog);
  };

  dialog.append(
    el('div', { class: 'modal-header' },
      el('h2', { class: 'modal-title', text: title }),
      el('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Закрыть', onclick: close }, icon('close', { size: 18 })),
    ),
    el('div', { class: 'modal-body' }, body),
    actions.length
      ? el('div', { class: 'modal-footer' },
          ...actions.map((action) =>
            button(action.label, {
              variant: action.variant || 'secondary',
              onClick: async (event) => {
                if (action.keepOpen) {
                  await action.onClick?.(event, close);
                } else {
                  close();
                  await action.onClick?.(event);
                }
              },
            }),
          ),
        )
      : null,
  );

  const backdrop = el('div', { class: 'modal-backdrop', onclick: (event) => event.target === backdrop && close() }, dialog);
  document.body.append(backdrop);
  document.body.style.overflow = 'hidden';
  document.addEventListener('keydown', onKey, true);

  // Focus the first meaningful control, not the close button.
  requestAnimationFrame(() => {
    const target = dialog.querySelector('input, textarea, select, button:not([aria-label=t("Закрыть")])');
    target?.focus();
  });

  return { close, dialog };
}

function trapFocus(event, container) {
  const focusable = container.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
  );
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

/* -------------------------------------------------------------------------- */
/* Tabs                                                                        */
/* -------------------------------------------------------------------------- */

export function tabs({ items, active, onChange }) {
  const list = el('div', { class: 'tabs', role: 'tablist' });
  const buttons = items.map((item) => {
    const node = el('button', {
      class: 'tab',
      type: 'button',
      role: 'tab',
      'aria-selected': item.value === active ? 'true' : 'false',
      onclick: () => {
        for (const other of buttons) other.setAttribute('aria-selected', 'false');
        node.setAttribute('aria-selected', 'true');
        onChange?.(item.value);
      },
    }, item.label, item.count !== undefined ? el('span', { class: 'text-muted', style: { marginLeft: '6px' }, text: String(item.count) }) : null);
    return node;
  });
  list.append(...buttons);
  return list;
}

/* -------------------------------------------------------------------------- */
/* Person row                                                                  */
/* -------------------------------------------------------------------------- */

export function personRow(person, { action = null, subtitle = null } = {}) {
  return el('div', { class: 'person-card' },
    avatar(person),
    el('div', { class: 'info' },
      el('div', { class: 'name' },
        el('a', { href: `/u/${encodeURIComponent(person.username)}` }, person.display_name || person.username),
        person.is_verified ? icon('verified', { size: 14, title: 'Подтверждённый аккаунт' }) : null,
      ),
      el('div', { class: 'handle', text: `@${person.username}` }),
      subtitle || (person.bio ? el('p', { class: 'bio clamp-2', text: person.bio }) : null),
    ),
    action ? el('div', { class: 'list-row-action' }, action) : null,
  );
}

/* -------------------------------------------------------------------------- */
/* Misc                                                                        */
/* -------------------------------------------------------------------------- */

export function statGrid(items) {
  return el('div', { class: 'stat-grid' },
    ...items.map((item) =>
      el('div', { class: 'stat' },
        el('div', { class: 'stat-value', text: typeof item.value === 'number' ? compactNumber(item.value) : String(item.value) }),
        el('div', { class: 'stat-label', text: item.label }),
      ),
    ),
  );
}

export function counter(node, max) {
  const update = () => {
    const length = node.value.length;
    const ratio = length / max;
    node.parentElement.querySelector('.counter')?.remove();
    if (!max) return;
    const label = el('span', {
      class: `counter ${ratio >= 1 ? 'is-exceeded' : ratio > 0.9 ? 'is-warning' : ''}`,
      text: `${length} / ${max}`,
    });
    node.insertAdjacentElement('afterend', label);
  };
  node.addEventListener('input', update);
  update();
  return () => node.parentElement.querySelector('.counter')?.remove();
}

/** Copy text to the clipboard with a graceful fallback. */
export async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Clipboard API needs a secure context; fall back to a temporary textarea.
    const area = el('textarea', { style: { position: 'fixed', opacity: '0' } });
    area.value = text;
    document.body.append(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    return ok;
  }
}

export { plural };
