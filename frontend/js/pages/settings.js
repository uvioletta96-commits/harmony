/**
 * Settings: profile, privacy, notifications, account, data.
 *
 * Every section maps to a distinct API surface so that a user can see exactly
 * which control affects which piece of data — the transparency the GDPR
 * articles are actually asking for.
 */

import { locale } from '../core/format.js';
import i18n, { t } from '../core/i18n.js';
import { LOCALE_LIST, isSupported } from '../i18n/index.js';

import api from '../core/api.js';
import { el, clear, $ } from '../core/dom.js';
import { icon } from '../core/icons.js';
import store, { logout, patchCurrentUser } from '../core/store.js';
import toast from '../core/toast.js';
import { router } from '../core/router.js';
import { applyServerErrors } from '../core/validate.js';
import { button, setLoading, toggle, select, alert, avatar, statGrid, loadingRow, modal, copyToClipboard } from '../components/ui.js';
import { confirmDialog } from '../components/report.js';

const SECTIONS = [
  { id: 'profile', label: t('Профиль') },
  { id: 'privacy', label: t('Приватность') },
  { id: 'notifications', label: t('Уведомления') },
  { id: 'account', label: t('Аккаунт') },
  { id: 'language', label: t('Язык интерфейса') },
  { id: 'data', label: t('Данные и приватность') },
  { id: 'sessions', label: t('Устройства') },
];

export async function render({ section } = {}) {
  const currentUser = store.get('currentUser');
  if (!currentUser) {
    router.navigate('/login', { replace: true });
    return { node: el('div') };
  }

  const active = SECTIONS.some((item) => item.id === section) ? section : 'profile';
  const body = el('div', { class: 'panel', id: 'settings-body' });
  const nav = el('nav', { class: 'settings-nav', 'aria-label': t('Разделы настроек') });

  for (const item of SECTIONS) {
    nav.append(
      el('a', {
        href: `/settings/${item.id}`,
        'aria-current': item.id === active ? 'page' : undefined,
        text: item.label,
      }),
    );
  }

  const shell = el('div', { class: 'settings-layout' }, nav, body);
  const loader = { profile: profileSection, privacy: privacySection, notifications: notificationsSection, language: languageSection, account: accountSection, data: dataSection, sessions: sessionsSection };
  body.append(loadingRow());
  loader[active](body, currentUser);

  document.title = t('{v0} · Настройки · Гармония', { v0: SECTIONS.find((item) => item.id === active)?.label });
  return { node: el('div', { class: 'layout-wide', style: { margin: '0 auto' } }, shell), wide: true };
}

function panel(title, description, ...children) {
  return el('div', {},
    el('div', { class: 'panel-header' },
      el('div', {},
        el('h1', { class: 'panel-title', text: title }),
        description ? el('p', { class: 'hint', style: { marginTop: '2px' }, text: description }) : null,
      ),
    ),
    el('div', { class: 'panel-body' }, ...children),
  );
}

function settingRow(label, hint, control) {
  return el('div', { class: 'setting-row' },
    el('div', { class: 'setting-text' },
      el('div', { class: 'setting-label', text: label }),
      hint ? el('div', { class: 'setting-hint', text: hint }) : null,
    ),
    el('div', { class: 'setting-control' }, control),
  );
}

/* -------------------------------------------------------------------------- */
/* Profile                                                                     */
/* -------------------------------------------------------------------------- */

async function profileSection(host, user) {
  clear(host);
  const data = await api.get(`/users/${user.public_id}`);
  const profile = data.user;

  const displayName = el('input', { class: 'input', value: profile.display_name || '', maxlength: '64', id: 's-display' });
  const bio = el('textarea', { class: 'textarea', rows: '4', maxlength: '600', id: 's-bio' });
  bio.value = profile.bio || '';
  const location = el('input', { class: 'input', value: profile.location || '', maxlength: '96', id: 's-location' });
  const pronouns = el('input', { class: 'input', value: profile.pronouns || '', maxlength: '32', id: 's-pronouns' });
  const website = el('input', { class: 'input', type: 'url', value: profile.website || '', placeholder: 'https://', id: 's-website' });
  const colour = el('select', { class: 'select', id: 's-colour' });
  for (const [value, label] of [['sand', t('Песочный')], ['stone', t('Каменный')], ['sage', t('Шалфей')], ['clay', t('Глина')], ['dusk', t('Сумерки')], ['linen', t('Лён')], ['moss', t('Мох')], ['ash', t('Пепел')]]) {
    const option = el('option', { value }, label);
    if ((profile.avatar_color || 'sand') === value) option.selected = true;
    colour.append(option);
  }

  const preview = el('div', { style: { display: 'flex', gap: 'var(--space-4)', alignItems: 'center', marginBottom: 'var(--space-5)' } },
    el('div', { id: 'avatar-preview' }, avatar({ ...profile }, { size: 'xl', link: false })),
    el('div', {},
      el('p', { style: { fontSize: 'var(--text-md)', fontWeight: '500' }, text: profile.display_name || profile.username }),
      el('p', { class: 'text-muted text-sm', text: `@${profile.username}` }),
      el('p', { class: 'hint', style: { marginTop: '4px' }, text: t('Цвет подставляется автоматически, если не выбран.') }),
    ),
  );

  colour.addEventListener('change', () => {
    const node = document.getElementById('avatar-preview');
    if (!node) return;
    const clone = avatar({ ...profile, avatar_color: colour.value }, { size: 'xl', link: false });
    clear(node).append(clone);
  });

  const save = button(t('Сохранить'), { variant: 'primary', onClick: persist });

  async function persist(event) {
    setLoading(save, true);
    try {
      const result = await api.patch(`/users/${user.public_id}`, {
        display_name: displayName.value.trim(),
        bio: bio.value.trim(),
        location: location.value.trim(),
        pronouns: pronouns.value.trim(),
        website: website.value.trim(),
        avatar_color: colour.value,
      });
      patchCurrentUser(result.user);
      toast.success(t('Профиль обновлён'));
    } catch (error) {
      if (error.fields) applyServerErrors(el('form'), error.fields);
      toast.error(error.message);
    } finally {
      setLoading(save, false);
    }
  }

  const avatarUpload = el('div', { class: 'setting-row' },
    el('div', { class: 'setting-text' },
      el('div', { class: 'setting-label', text: t('Аватар') }),
      el('div', { class: 'setting-hint', text: t('JPEG, PNG, WebP или GIF, до 8 МБ. Из EXIF удаляются данные о местоположении.') }),
    ),
    el('div', { class: 'setting-control' },
      button(t('Загрузить'), { variant: 'secondary', size: 'sm', onClick: (event) => uploadAvatar(event.currentTarget, user) }),
    ),
  );

  host.append(
    panel(t('Профиль'), t('Так вас видят другие участники.'), preview, avatarUpload, el('hr', { style: { margin: 'var(--space-5) 0' } }),
      el('div', { class: 'field' }, el('label', { class: 'label', for: 's-display' }, t('Отображаемое имя')), displayName, el('p', { class: 'error-text hidden' })),
      el('div', { class: 'field' }, el('label', { class: 'label', for: 's-username' }, t('Имя пользователя')),
        el('input', { class: 'input', value: `@${profile.username}`, disabled: true, id: 's-username' }),
        el('p', { class: 'hint', text: t('Имя пользователя менять нельзя — по нему вас находят и упоминают.') }),
      ),
      el('div', { class: 'field' }, el('label', { class: 'label', for: 's-bio' }, t('О себе')), bio, el('p', { class: 'hint', text: t('До 600 символов.') }), el('p', { class: 'error-text hidden' })),
      el('div', { class: 'field' }, el('label', { class: 'label', for: 's-location' }, t('Местоположение')), location, el('p', { class: 'hint', text: t('Город или страна — по желанию.') })),
      el('div', { class: 'field' }, el('label', { class: 'label', for: 's-pronouns' }, t('Местоимения')), pronouns, el('p', { class: 'hint', text: t('Необязательно.') })),
      el('div', { class: 'field' }, el('label', { class: 'label', for: 's-website' }, t('Сайт')), website, el('p', { class: 'error-text hidden' })),
      el('div', { class: 'field' }, el('label', { class: 'label', for: 's-colour' }, t('Цвет аватара')), colour),
      el('div', { style: { display: 'flex', gap: 'var(--space-2)', justifyContent: 'flex-end' } }, save),
    ),
  );
}

/**
 * Interface language.
 *
 * The list is the set of fully translated catalogues, and every entry is shown
 * with the language's own name - a reader who does not read the current
 * language still recognises theirs among the rows, which an English-only list
 * denies them.
 *
 * Choosing here does two things: it applies the language to this browser
 * immediately, and it stores it on the account so the same choice follows the
 * reader to another device. The account value is what a sign-in resolves from,
 * so a browser guess never silently overwrites a real choice.
 */
async function languageSection(host, currentUser) {
  clear(host);
  const select = el('select', { class: 'input', id: 's-language', name: 'language' });

  // "Follow the browser" is the default and is a real option, not an absence of
  // one: it is what the account says when nobody has chosen anything.
  select.append(el('option', { value: '', text: t('Как в браузере') }));
  for (const locale of LOCALE_LIST) {
    select.append(el('option', {
      value: locale.code,
      text: `${locale.native} — ${locale.english}`,
      selected: currentUser.language === locale.code,
    }));
  }
  select.value = isSupported(currentUser.language) ? currentUser.language : '';

  const status = el('p', { class: 'hint', style: { marginTop: 'var(--space-2)' } });
  const save = button(t('Сохранить'), {
    variant: 'primary',
    onClick: async (event) => {
      const chosen = select.value;
      setLoading(event.currentTarget, true);
      try {
        await api.patch(`/users/${currentUser.public_id}`, { language: chosen || null });
        store.patchCurrentUser({ ...currentUser, language: chosen || null });
        // Apply locally first so the switch is instant, then let the re-render
        // confirm it against the server rather than the other way round.
        if (chosen) i18n.setLanguage(chosen);
        else i18n.syncWithProfile(null);
        toast.success(t('Настройка сохранена'));
        router.resolve();
      } catch (error) {
        toast.error(error.message);
      } finally {
        setLoading(event.currentTarget, false);
      }
    },
  });

  select.addEventListener('change', () => {
    // Preview before saving: a language is easier to judge from a whole page
    // than from a word on a dropdown, and a change this cheap does not need a
    // round trip to be useful.
    if (select.value) i18n.setLanguage(select.value);
    status.textContent = select.value
      ? t('Интерфейс переключится сразу после сохранения.')
      : t('Интерфейс будет на языке браузера.');
  });

  // `host`, not `body`: `body` does not exist in this scope, so the append
  // raised a ReferenceError *after* `clear(host)` had already emptied the
  // panel. The section rendered as nothing at all.
  host.append(
    panel(t('Язык интерфейса'), t('Каждый язык переведён полностью — частично переведённых вариантов здесь нет.'),
      el('div', { class: 'field' },
        el('label', { class: 'label', for: 's-language' }, t('Язык')),
        select,
        el('p', { class: 'hint', text: t('Выбор сохраняется в аккаунте и действует на всех устройствах.') }),
        status,
      ),
      el('div', { style: { marginTop: 'var(--space-4)' } }, save),
    ),
  );
}

/**
 * Replace the reader's avatar.
 *
 * Posts the bytes to ``/users/me/avatar`` and nothing else. The earlier version
 * uploaded the file and then sent ``{avatar_url: <url from the upload
 * response>}`` to the profile endpoint - and the profile schema has no such
 * field, so the update was accepted, discarded by ``ignore_unknown``, and the
 * page reported success while the avatar never changed.
 *
 * Letting the client name a URL would be worse than broken: every profile page
 * would then load an image from wherever the reader chose. The endpoint builds
 * the URL from the bytes it stored.
 */
async function uploadAvatar(node, user) {
  const input = el('input', { type: 'file', accept: 'image/jpeg,image/png,image/webp,image/gif', class: 'visually-hidden' });
  document.body.append(input);

  input.addEventListener('change', async () => {
    const file = input.files?.[0];
    input.remove();
    if (!file) return;
    setLoading(node, true);
    try {
      const body = new FormData();
      body.append('file', file);
      const result = await api.upload('/me/avatar', body);
      store.patchCurrentUser({ ...store.get('currentUser'), avatar_url: result.avatar_url });
      toast.success(t('Аватар обновлён'));
      router.resolve();
    } catch (error) {
      toast.error(error.message);
    } finally {
      setLoading(node, false);
    }
  });

  input.click();
}

/* -------------------------------------------------------------------------- */
/* Privacy                                                                     */
/* -------------------------------------------------------------------------- */

async function privacySection(host, user) {
  clear(host);
  host.append(loadingRow());

  let settings;
  try {
    settings = (await api.get('/me/privacy')).privacy;
  } catch (error) {
    clear(host);
    host.append(alert({ variant: 'danger', text: error.message }));
    return;
  }

  const makeToggle = (key, label, hint) => {
    const control = toggle({ name: key, label, hint, checked: Boolean(settings[key]) });
    control.querySelector('input').addEventListener('change', async (event) => {
      try {
        const result = await api.patch('/me/privacy', { [key]: event.target.checked });
        settings = result.privacy;
        toast.success(t('Настройка сохранена'), { duration: 2000 });
      } catch (error) {
        event.target.checked = !event.target.checked;
        toast.error(error.message);
      }
    });
    return control;
  };

  const visibility = select({
    name: 'profile_visibility',
    label: t('Кто видит ваш профиль'),
    options: [
      { value: 'public', label: t('Все — профиль и публикации видны всем') },
      { value: 'followers', label: t('Только подписчики') },
      { value: 'private', label: t('Только вы — профиль скрыт, но посты видны по ссылке') },
    ],
    value: settings.profile_visibility || 'public',
  });
  visibility.querySelector('select').addEventListener('change', async (event) => {
    try {
      const result = await api.patch('/me/privacy', { profile_visibility: event.target.value });
      settings = result.privacy;
      toast.success(t('Видимость профиля обновлена'));
    } catch (error) {
      toast.error(error.message);
    }
  });

  clear(host);
  host.append(
    panel(t('Приватность'), t('Вы решаете, кто видит ваш профиль и активность.'),
      visibility,
      el('hr', { style: { margin: 'var(--space-5) 0' } }),
      settingRow(t('Показывать в поиске'), t('Вас смогут найти по имени пользователя и описанию.'), makeToggle('discoverable_by_search', t('Показывать в поиске'))),
      settingRow(t('Показывать адрес почты'), t('Виден только тем, кто видит ваш профиль.'), makeToggle('show_email', t('Показывать почту'))),
      settingRow(t('Показывать время последнего визита'), t('Когда вы были в сети.'), makeToggle('show_last_seen', t('Показывать активность'))),
      settingRow(t('Сообщения от незнакомцев'), t('Кто может вам написать первым.'), makeToggle('allow_messages_from_anyone', t('Разрешить сообщения'))),
      settingRow(t('Упоминания'), t('Могут ли другие отмечать вас в публикациях.'), makeToggle('allow_mentions', t('Разрешить упоминания'))),
      settingRow(t('Персонализация ленты'), t('Показывать в ленте публикации тех, на кого вы подписаны.'), makeToggle('personalize_feed', t('Персонализировать ленту'))),
    ),
  );
}

/* -------------------------------------------------------------------------- */
/* Notifications                                                               */
/* -------------------------------------------------------------------------- */

async function notificationsSection(host, user) {
  clear(host);
  let settings = {};
  try {
    settings = (await api.get('/me/privacy')).privacy;
  } catch { /* fall back to defaults */ }

  const makeToggle = (key, label, hint) => {
    const control = toggle({ name: key, label, hint, checked: Boolean(settings[key]) });
    control.querySelector('input').addEventListener('change', async (event) => {
      try {
        await api.patch('/me/privacy', { [key]: event.target.checked });
        toast.success(t('Сохранено'), { duration: 2000 });
      } catch (error) {
        event.target.checked = !event.target.checked;
        toast.error(error.message);
      }
    });
    return control;
  };

  host.append(
    panel(t('Уведомления'), t('Что приходит на почту, а что — прямо в интерфейс.'),
      settingRow(t('Письма о событиях'), t('Ответы на ваши публикации, комментарии и сообщения.'), makeToggle('email_notifications', t('Событийные письма'))),
      settingRow(t('Новости сообщества'), t('Редкие письма о новых возможностях. Можно отписаться в любой момент.'), makeToggle('marketing_consent', t('Новости и анонсы'))),
      el('div', { style: { marginTop: 'var(--space-5)' } },
        alert({
          variant: 'info',
          title: t('Отписаться мгновенно'),
          text: t('Нажмите «Отозвать согласие», и мы прекратим маркетинговые рассылки немедленно, сохранив запись в истории согласий.'),
        }),
        el('div', { style: { marginTop: 'var(--space-3)' } },
          button(t('Отозвать согласие на рассылки'), {
            variant: 'danger-secondary',
            size: 'sm',
            onClick: async (event) => {
              setLoading(event.currentTarget, true);
              try {
                await api.post('/me/data/consent');
                toast.success(t('Согласие отозвано'));
                router.resolve();
              } catch (error) {
                toast.error(error.message);
              } finally {
                setLoading(event.currentTarget, false);
              }
            },
          }),
        ),
      ),
    ),
  );
}

/* -------------------------------------------------------------------------- */
/* Account                                                                     */
/* -------------------------------------------------------------------------- */

async function accountSection(host, user) {
  clear(host);

  const currentPassword = el('input', { class: 'input', type: 'password', autocomplete: 'current-password', id: 'a-cur' });
  const newPassword = el('input', { class: 'input', type: 'password', autocomplete: 'new-password', id: 'a-new' });
  const repeat = el('input', { class: 'input', type: 'password', autocomplete: 'new-password', id: 'a-rep' });
  const errorSlot = el('div');
  const changeButton = button(t('Изменить пароль'), { variant: 'primary', onClick: change });

  async function change() {
    clear(errorSlot);
    if (newPassword.value !== repeat.value) {
      errorSlot.append(alert({ variant: 'warning', text: t('Новые пароли не совпадают.') }));
      return;
    }
    setLoading(changeButton, true);
    try {
      const result = await api.post('/auth/change-password', {
        current_password: currentPassword.value,
        new_password: newPassword.value,
      });
      toast.success(result.message);
      currentPassword.value = '';
      newPassword.value = '';
      repeat.value = '';
      // Every session was revoked, so the current one is gone too.
      setTimeout(() => logout(), 1800);
    } catch (error) {
      errorSlot.append(alert({ variant: 'danger', text: error.message }));
    } finally {
      setLoading(changeButton, false);
    }
  }

  host.append(
    panel(t('Аккаунт'), t('Данные для входа и управление сессиями.'),
      errorSlot,
      el('div', { class: 'field' }, el('label', { class: 'label', for: 'a-cur' }, t('Текущий пароль')), currentPassword),
      el('div', { class: 'field' }, el('label', { class: 'label', for: 'a-new' }, t('Новый пароль')), newPassword,
        el('p', { class: 'hint', text: t('Минимум 10 символов, минимум два типа символов. Не используйте пароль с другого сайта.') })),
      el('div', { class: 'field' }, el('label', { class: 'label', for: 'a-rep' }, t('Повторите новый пароль')), repeat),
      el('div', { style: { display: 'flex', justifyContent: 'flex-end', marginBottom: 'var(--space-5)' } }, changeButton),
      el('hr', { style: { margin: 'var(--space-5) 0' } }),
      settingRow(t('Выйти из аккаунта'), t('Завершить сессию на этом устройстве.'),
        button(t('Выйти'), { variant: 'secondary', onClick: () => logout() })),
    ),
  );
}

/* -------------------------------------------------------------------------- */
/* Data rights (GDPR)                                                          */
/* -------------------------------------------------------------------------- */

async function dataSection(host, user) {
  clear(host);
  host.append(loadingRow());

  const body = el('div');

  const exportButton = button(t('Выгрузить мои данные'), {
    variant: 'primary',
    onClick: async (event) => {
      setLoading(event.currentTarget, true);
      try {
        const result = await api.post('/me/data/export');
        toast.success(result.message);
        downloadLink.hidden = false;
        downloadLink.href = `/api/v1/me/data/export/${result.request.id}`;
      } catch (error) {
        toast.error(error.message);
      } finally {
        setLoading(event.currentButton || event.currentTarget, false);
      }
    },
  });

  const downloadLink = el('a', { class: 'btn btn-secondary', href: '#', hidden: true, download: '', text: t('Скачать файл') });

  let requests = [];
  let register = null;
  try {
    const [list, reg] = await Promise.all([api.get('/me/data/requests'), api.get('/me/privacy/register')]);
    requests = list.requests;
    register = reg;
  } catch { /* the rest of the page still renders */ }

  clear(host);

  body.append(
    panel(t('Мои данные'), t('Право на доступ и переносимость (ст. 15 и 20 GDPR).'),
      el('p', { class: 'hint', style: { marginBottom: 'var(--space-4)' } },
        t('Выгрузка содержит профиль, все публикации, комментарии, историю согласий и счётчики — в машиночитаемом формате JSON, готовом для переноса в другую систему.')),
      el('div', { style: { display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap' } }, exportButton, downloadLink),
      requests.length
        ? el('div', { style: { marginTop: 'var(--space-5)' } },
            el('h2', { style: { fontSize: 'var(--text-base)', fontWeight: '500', marginBottom: 'var(--space-3)' }, text: t('История запросов') }),
            el('div', { class: 'panel', style: { boxShadow: 'none' } },
              ...requests.slice(0, 8).map((request) =>
                el('div', { class: 'list-row' },
                  el('div', { style: { flex: '1' } },
                    el('div', { class: 'list-row-title', text: KIND_LABELS[request.kind] || request.kind }),
                    el('div', { class: 'list-row-sub', text: t('Создан {v0}', { v0: new Date(request.created_at).toLocaleDateString(locale()) }) }),
                  ),
                  el('span', { class: `badge ${request.status === 'completed' ? 'badge-success' : 'badge-neutral'}`, text: STATUS_LABELS[request.status] || request.status }),
                  request.download_url
                    ? el('a', { class: 'btn btn-ghost btn-sm', href: request.download_url, download: '', text: t('Скачать') })
                    : null,
                ),
              ),
            ),
          )
        : null,
    ),

    panel(t('Ограничение и возражение'), t('Отдельные механизмы, предусмотренные GDPR.'),
      el('div', { style: { display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap' } },
        button(t('Ограничить обработку'), { variant: 'secondary', onClick: () => dataAction('restriction', t('Обработка ограничена')) }),
        button(t('Выразить возражение'), { variant: 'secondary', onClick: () => dataAction('objection', t('Возражение зарегистрировано')) }),
      ),
      el('p', { class: 'hint', style: { marginTop: 'var(--space-3)' } },
        t('Ограничение приостанавливает необязательную обработку при споре. Возражение прекращает обработку для целей, на которые у вас нет согласия.')),
    ),

    register
      ? panel(t('Какие данные обрабатываются'), t('Реестр операций обработки (ст. 30 GDPR).'),
          el('div', { class: 'table-wrap' },
            el('table', { class: 'table' },
              el('thead', {}, el('tr', {},
                el('th', {}, t('Цель')),
                el('th', {}, t('Правовое основание')),
                el('th', {}, t('Срок хранения')),
              )),
              el('tbody', {},
                ...register.activities.map((activity) =>
                  el('tr', {},
                    el('td', { text: activity.purpose }),
                    el('td', { class: 'text-sm', text: activity.legal_basis }),
                    el('td', { class: 'text-sm', text: activity.retention }),
                  ),
                ),
              ),
            ),
          ),
        )
      : null,

    panel(t('Удаление аккаунта'), t('Необратимо. Сначала даётся льготный период, в течение которого удаление можно отменить.'),
      el('div', { class: 'panel panel-pad danger-zone', style: { boxShadow: 'none' } },
        el('p', { class: 'hint', style: { marginBottom: 'var(--space-3)' } },
          t('Будут удалены публикации, комментарии, отметки «Нравится», личные сообщения, загруженные файлы, сессии и токены. Запись о санкциях сохранится без связи с аккаунтом — это требование законодательства о модерации.')),
        button(t('Удалить аккаунт'), { variant: 'danger', onClick: startDeletion }),
      ),
    ),
  );

  host.append(body);

  async function dataAction(kind, successMessage) {
    try {
      await api.post(`/me/data/${kind}`);
      toast.success(successMessage);
      router.resolve();
    } catch (error) {
      toast.error(error.message);
    }
  }

  async function startDeletion() {
    const confirmed = await confirmDialog({
      title: t('Удалить аккаунт?'),
      message: t('Аккаунт перестанет работать сразу, а данные будут удалены необратимо после льготного периода. В течение этого периода удаление можно отменить.'),
      confirmLabel: t('Продолжить'),
    });
    if (!confirmed) return;

    // Step-up authentication: a hijacked session alone must not destroy an
    // account, so the password is required even though the user is signed in.
    const password = el('input', { class: 'input', type: 'password', autocomplete: 'current-password', placeholder: t('Ваш пароль') });
    const dialog = modal({
      title: t('Подтвердите паролем'),
      body: el('div', {},
        el('p', { class: 'hint', style: { marginBottom: 'var(--space-3)' }, text: t('Это последний шаг. Введите пароль, чтобы подтвердить, что удаляете аккаунт именно вы.') }),
        password,
      ),
      actions: [
        { label: t('Отмена'), variant: 'ghost' },
        {
          label: t('Запланировать удаление'),
          variant: 'danger',
          keepOpen: true,
          onClick: async (event, close) => {
            setLoading(event.currentTarget, true);
            try {
              const reauth = await api.post('/auth/reauthenticate', { password: password.value });
              const result = await api.post('/me/deletion', { reauth_token: reauth.reauth_token });
              close();
              toast.success(result.message, { duration: 8000 });
              setTimeout(() => logout(), 4000);
            } catch (error) {
              toast.error(error.message);
            } finally {
              setLoading(event.currentTarget, false);
            }
          },
        },
      ],
    });
    requestAnimationFrame(() => password.focus());
  }
}

const KIND_LABELS = {
  export: t('Выгрузка данных'),
  erase: t('Удаление'),
  restriction: t('Ограничение обработки'),
  rectify: t('Исправление данных'),
  object: t('Возражение'),
};
const STATUS_LABELS = {
  pending: t('в ожидании'),
  processing: t('в работе'),
  completed: t('выполнено'),
  rejected: t('отклонено'),
  cancelled: t('отменено'),
  failed: t('ошибка'),
};

/* -------------------------------------------------------------------------- */
/* Sessions                                                                    */
/* -------------------------------------------------------------------------- */

async function sessionsSection(host, user) {
  clear(host);
  host.append(loadingRow());

  let sessions = [];
  try {
    sessions = (await api.get('/auth/sessions')).sessions;
  } catch (error) {
    clear(host);
    host.append(alert({ variant: 'danger', text: error.message }));
    return;
  }

  clear(host);
  host.append(
    panel(t('Активные устройства'), t('Завершите сессию, если не узнаёте устройство.'),
      el('div', { style: { display: 'flex', gap: 'var(--space-2)', marginBottom: 'var(--space-5)', flexWrap: 'wrap' } },
        button(t('Завершить все остальные сессии'), {
          variant: 'danger-secondary',
          onClick: async () => {
            const confirmed = await confirmDialog({
              title: t('Завершить все сессии?'),
              message: t('Вы выйдете из аккаунта на всех устройствах, включая это. Потребуется войти заново.'),
              confirmLabel: t('Завершить всё'),
            });
            if (!confirmed) return;
            try {
              await api.post('/auth/logout', { all_sessions: true });
              toast.success(t('Все сессии завершены'));
              setTimeout(() => logout(), 1200);
            } catch (error) {
              toast.error(error.message);
            }
          },
        }),
      ),
      el('div', { class: 'panel', style: { boxShadow: 'none' } },
        ...sessions.map((session) =>
          el('div', { class: 'list-row' },
            el('span', { style: { flex: '1', minWidth: '0' } },
              el('div', { class: 'list-row-title' },
                shortenAgent(session.user_agent),
                session.is_current ? el('span', { class: 'badge badge-accent', style: { marginLeft: '8px' }, text: t('текущее') }) : null,
              ),
              el('div', { class: 'list-row-sub', text: t('Вход {v0}', { v0: new Date(session.created_at).toLocaleString(locale()) }) }),
            ),
            !session.is_current
              ? el('button', {
                  class: 'btn btn-ghost btn-sm',
                  type: 'button',
                  text: t('Завершить'),
                  onclick: async () => {
                    try {
                      await api.delete(`/auth/sessions/${session.jti}`);
                      toast.success(t('Сессия завершена'));
                      sessionsSection(host, user);
                    } catch (error) {
                      toast.error(error.message);
                    }
                  },
                })
              : null,
          ),
        ),
      ),
    ),
  );
}

function shortenAgent(agent) {
  if (!agent) return t('Неизвестное устройство');
  const browser = /Firefox\/[\d.]+/.test(agent) ? 'Firefox'
    : /Edg\//.test(agent) ? 'Edge'
    : /Chrome\//.test(agent) ? 'Chrome'
    : /Safari\//.test(agent) && !/Chrome/.test(agent) ? 'Safari'
    : t('Браузер');
  const os = /Android/.test(agent) ? 'Android'
    : /iPhone|iPad/.test(agent) ? 'iOS'
    : /Mac OS X/.test(agent) ? 'macOS'
    : /Windows/.test(agent) ? 'Windows'
    : /Linux/.test(agent) ? 'Linux'
    : '';
  return os ? `${browser} · ${os}` : browser;
}
