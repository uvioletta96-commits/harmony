/**
 * Moderator panel: report queue, enforcement actions, user administration.
 */

import { t } from '../core/i18n.js';

import api from '../core/api.js';
import { el, clear } from '../core/dom.js';
import { icon } from '../core/icons.js';
import store from '../core/store.js';
import toast from '../core/toast.js';
import { router } from '../core/router.js';
import { relativeTime, absoluteTime, compactNumber } from '../core/format.js';
import { avatar, button, setLoading, emptyState, errorState, loadingRow, alert, modal, statGrid, select } from '../components/ui.js';
import { confirmDialog } from '../components/report.js';

const SECTIONS = [
  { id: 'queue', label: t('Очередь жалоб') },
  { id: 'users', label: t('Пользователи') },
  { id: 'actions', label: t('История действий') },
];

const ACTIONS = [
  { value: 'none', label: t('Отклонить жалобу — контент оставить') },
  { value: 'warn', label: t('Предупреждение автору') },
  { value: 'hide', label: t('Скрыть до проверки') },
  { value: 'remove', label: t('Удалить контент') },
  { value: 'restrict', label: t('Приостановить аккаунт') },
  { value: 'suspend', label: t('Длительная приостановка') },
  { value: 'ban', label: t('Заблокировать навсегда') },
  { value: 'restore', label: t('Восстановить контент') },
];

const REASON_LABELS = {
  spam: t('Спам'), harassment: t('Оскорбления'), hate: t('Язык ненависти'), violence: t('Насилие'),
  sexual_content: t('Неприемлемый контент'), self_harm: t('Самоповреждение'),
  misinformation: t('Достоверность'), impersonation: t('Имперсонация'),
  copyright: t('Авторские права'), privacy: t('Личные данные'), other: t('Другое'),
};

export async function render({ section } = {}) {
  if (!store.isModerator()) {
    return {
      node: emptyState({
        iconName: 'lock',
        title: t('Доступ ограничен'),
        text: t('Раздел доступен только модераторам и администраторам.'),
        action: button(t('Вернуться в ленту'), { variant: 'secondary', onClick: () => router.navigate('/') }),
      }),
    };
  }

  const active = SECTIONS.some((item) => item.id === section) ? section : 'queue';
  const nav = el('nav', { class: 'settings-nav' });
  for (const item of SECTIONS) {
    nav.append(el('a', { href: `/admin/${item.id}`, 'aria-current': item.id === active ? 'page' : undefined, text: item.label }));
  }
  if (store.isAdmin()) {
    nav.append(el('a', { href: '/admin/moderators', text: t('Роли') }));
  }

  const body = el('div', { id: 'admin-body' });
  // A moderation queue is a table. A table in 456px is not a table.
  const shell = el('div', { class: 'layout-wide', style: { margin: '0 auto' } },
    el('div', { class: 'panel', style: { marginBottom: 'var(--space-4)' } },
      el('div', { class: 'panel-header' },
        el('div', {},
          el('h1', { class: 'panel-title' }, icon('shield', { size: 16, className: 'text-muted' }), t(' Панель модератора')),
          el('p', { class: 'hint', style: { marginTop: '2px' }, text: t('Все решения фиксируются в журнале и могут быть обжалованы.') }),
        ),
      ),
    ),
    el('div', { class: 'settings-layout' }, nav, body),
  );

  body.append(loadingRow());
  if (active === 'queue') queueSection(body);
  else if (active === 'users') usersSection(body);
  else if (active === 'actions') actionsSection(body);
  else if (active === 'moderators') rolesSection(body);

  return { node: shell, wide: true };
}

/* -------------------------------------------------------------------------- */
/* Dashboard + queue                                                           */
/* -------------------------------------------------------------------------- */

async function queueSection(host) {
  clear(host);
  host.append(loadingRow());

  let stats;
  try {
    stats = (await api.get('/admin/stats')).moderation;
  } catch (error) {
    clear(host);
    host.append(errorState({ text: error.message, onRetry: () => queueSection(host) }));
    return;
  }

  const filter = el('select', { class: 'select', style: { width: 'auto' }, 'aria-label': t('Статус жалоб') });
  for (const [value, label] of [['open', t('Новые')], ['in_review', t('В работе')], ['resolved', t('Решённые')], ['dismissed', t('Отклонённые')]]) {
    const option = el('option', { value }, label);
    if (value === 'open') option.selected = true;
    filter.append(option);
  }
  filter.addEventListener('change', () => load(filter.value));

  const list = el('div', { class: 'panel', style: { boxShadow: 'none' } });
  clear(host);

  host.append(
    el('div', { class: 'metric-grid' },
      metric(stats.open_reports, t('Открытых жалоб'), stats.open_reports > 0),
      metric(stats.posts_under_review, t('Постов на проверке'), stats.posts_under_review > 0),
      metric(stats.sanctioned_users, t('Ограниченных аккаунтов'), false),
      metric(stats.actions_last_24h, t('Действий за сутки'), false),
    ),
    el('div', { style: { display: 'flex', gap: 'var(--space-2)', marginBottom: 'var(--space-4)', alignItems: 'center' } },
      el('span', { class: 'text-sm text-muted', text: t('Показывать:') }),
      filter,
    ),
    list,
  );

  await load('open');

  async function load(status) {
    clear(list);
    list.append(loadingRow());
    try {
      const reports = await api.get(`/admin/reports?status=${status}`);
      if (!reports.length) {
        list.append(emptyState({ iconName: 'check', title: t('Очередь пуста'), text: t('Новых жалоб по этому фильтру нет.') }));
        return;
      }
      for (const report of reports) list.append(reportItem(report, () => queueSection(host)));
    } catch (error) {
      clear(list);
      list.append(errorState({ text: error.message, onRetry: () => load(status) }));
    }
  }
}

function metric(value, label, alert_) {
  return el('div', { class: `admin-metric ${alert_ ? 'is-alert' : ''}` },
    el('div', { class: 'value', text: String(value ?? 0) }),
    el('div', { class: 'label', text: label }),
  );
}

function reportItem(report, onDone) {
  const content = report.content || {};
  const author = report.author_history;

  const actionSelect = el('select', { class: 'select', style: { minWidth: '280px' }, 'aria-label': t('Действие') });
  for (const action of ACTIONS) actionSelect.append(el('option', { value: action.value }, action.label));

  const note = el('input', { class: 'input', placeholder: t('Комментарий для автора (необязательно)'), maxlength: '500' });
  const duration = el('input', { class: 'input', type: 'number', min: '1', max: '8760', placeholder: t('Часов'), style: { width: '110px' } });

  const apply = button(t('Применить'), {
    variant: 'primary',
    size: 'sm',
    onClick: async (event) => {
      const action = actionSelect.value;
      if (['restrict', 'suspend'].includes(action)) {
        const hours = Number(duration.value);
        if (!hours || hours < 1) {
          toast.warning(t('Укажите срок ограничения в часах.'));
          duration.focus();
          return;
        }
      }
      if (['ban', 'remove'].includes(action)) {
        const confirmed = await confirmDialog({
          title: t('Подтвердите действие'),
          message: action === 'ban'
            ? t('Аккаунт будет заблокирован, все сессии завершены. Действие можно отменить, но пользователь узнает о нём.')
            : t('Контент будет удалён и недоступен по прямой ссылке.'),
          confirmLabel: t('Применить'),
        });
        if (!confirmed) return;
      }
      setLoading(event.currentTarget, true);
      try {
        await api.post(`/admin/reports/${report.id}/resolve`, {
          action,
          note: note.value.trim(),
          duration_hours: ['restrict', 'suspend'].includes(action) ? Number(duration.value) : null,
        });
        toast.success(t('Решение применено и записано в журнал'));
        onDone();
      } catch (error) {
        toast.error(error.message);
        setLoading(event.currentTarget, false);
      }
    },
  });

  return el('div', { class: 'report-item' },
    el('div', { class: 'report-head' },
      el('span', { class: 'reason-chip badge badge-neutral', text: REASON_LABELS[report.reason] || report.reason }),
      el('span', { class: `badge ${report.severity === 'critical' || report.severity === 'high' ? 'badge-danger' : 'badge-neutral'}`, text: report.severity }),
      el('span', { class: 'badge badge-neutral', text: report.target_type }),
      report.duplicate_count > 1 ? el('span', { class: 'badge badge-warning', text: t('Жалоб: {v0}', { v0: report.duplicate_count }) }) : null,
      report.auto_flagged ? el('span', { class: 'badge badge-accent', text: t('скрыто автоматически') }) : null,
      el('span', { class: 'text-xs text-muted', style: { marginLeft: 'auto' }, title: absoluteTime(report.created_at), text: relativeTime(report.created_at) }),
    ),
    content.body ? el('blockquote', { class: 'report-content', text: content.body }) : el('p', { class: 'text-muted text-sm', text: t('Содержимое недоступно') }),
    author
      ? el('p', { class: 'text-xs text-muted', style: { marginTop: 'var(--space-2)' } },
          t('Автор: @{v0} · предупреждений: {v1} · предыдущих жалоб: {v2} · статус: {v3}', {v0: author.username, v1: author.warnings, v2: author.reports_against, v3: author.status }))
      : null,
    report.details ? el('p', { class: 'text-sm', style: { marginTop: 'var(--space-2)' } }, el('em', { text: `«${report.details}»` })) : null,
    el('div', { class: 'report-actions' },
      actionSelect,
      ['restrict', 'suspend'].includes(actionSelect.value) ? duration : null,
      note,
      apply,
    ),
  );
}

/* -------------------------------------------------------------------------- */
/* Users                                                                       */
/* -------------------------------------------------------------------------- */

async function usersSection(host) {
  clear(host);
  host.append(loadingRow());

  const search = el('input', { class: 'input', placeholder: t('Имя пользователя или отображаемое имя'), style: { maxWidth: '340px' } });
  const statusFilter = el('select', { class: 'select', style: { width: 'auto' } });
  for (const [value, label] of [['', t('Все')], ['active', t('Активные')], ['suspended', t('Приостановленные')], ['banned', t('Заблокированные')], ['pending', t('Не подтверждены')]]) {
    statusFilter.append(el('option', { value }, label));
  }

  const list = el('div', { class: 'panel', style: { boxShadow: 'none' } });
  const prefill = new URLSearchParams(location.search).get('q');
  if (prefill) search.value = prefill;

  clear(host);
  host.append(
    el('div', { style: { display: 'flex', gap: 'var(--space-2)', marginBottom: 'var(--space-4)', flexWrap: 'wrap' } }, search, statusFilter),
    list,
  );

  let timer = null;
  search.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(load, 320);
  });
  statusFilter.addEventListener('change', load);

  await load();

  async function load() {
    clear(list);
    list.append(loadingRow());
    const params = new URLSearchParams();
    if (search.value.trim()) params.set('q', search.value.trim());
    if (statusFilter.value) params.set('status', statusFilter.value);
    try {
      const users = await api.get(`/admin/users?${params}`);
      if (!users.length) {
        list.append(emptyState({ iconName: 'user', title: t('Никого не найдено') }));
        return;
      }
      for (const target of users) list.append(userRow(target, load));
    } catch (error) {
      clear(list);
      list.append(errorState({ text: error.message, onRetry: load }));
    }
  }
}

function userRow(target, reload) {
  const isSelf = target.public_id === store.get('currentUser')?.public_id;
  const actionSelect = el('select', { class: 'select', style: { minWidth: '200px' }, disabled: isSelf, 'aria-label': t('Действие') });
  for (const [value, label] of [
    ['warn', t('Предупреждение')],
    ['restrict', t('Ограничить')],
    ['suspend', t('Приостановить')],
    ['ban', t('Заблокировать')],
    ['unban', t('Снять ограничение')],
  ]) {
    actionSelect.append(el('option', { value }, label));
  }

  const duration = el('input', { class: 'input', type: 'number', min: '1', placeholder: t('Часов'), style: { width: '100px' } });
  const reason = el('input', { class: 'input', placeholder: t('Основание'), maxlength: '500' });

  return el('div', { class: 'list-row' },
    avatar(target, { size: 'sm', link: false }),
    el('div', { style: { minWidth: '0', flex: '1' } },
      el('div', { class: 'list-row-title' },
        target.display_name || target.username,
        el('span', { class: 'text-muted text-sm', style: { marginLeft: '6px' }, text: `@${target.username}` }),
      ),
      el('div', { class: 'list-row-sub' },
        t('{v0} · предупреждений: {v1} · жалоб: {v2}', {v0: target.status, v1: target.warnings, v2: target.reports_against }),
      ),
    ),
    actionSelect,
    duration,
    reason,
    button(t('Применить'), {
      variant: 'secondary',
      size: 'sm',
      disabled: isSelf,
      onClick: async (event) => {
        setLoading(event.currentTarget, true);
        try {
          await api.post(`/admin/users/${target.public_id}/action`, {
            action: actionSelect.value,
            reason: reason.value.trim(),
            duration_hours: ['restrict', 'suspend'].includes(actionSelect.value) ? Number(duration.value) || null : null,
          });
          toast.success(t('Действие применено'));
          reload();
        } catch (error) {
          toast.error(error.message);
          setLoading(event.currentTarget, false);
        }
      },
    }),
  );
}

/* -------------------------------------------------------------------------- */
/* Action history                                                              */
/* -------------------------------------------------------------------------- */

async function actionsSection(host) {
  clear(host);
  host.append(loadingRow());
  const list = el('div', { class: 'panel', style: { boxShadow: 'none' } });
  clear(host);
  host.append(list);

  try {
    const actions = await api.get('/admin/actions');
    if (!actions.length) {
      list.append(emptyState({ iconName: 'shield', title: t('Записей пока нет') }));
      return;
    }
    const table = el('table', { class: 'table' },
      el('thead', {}, el('tr', {},
        el('th', {}, t('Действие')),
        el('th', {}, t('Субъект')),
        el('th', {}, t('Объект')),
        el('th', {}, t('Модератор')),
        el('th', {}, t('Основание')),
        el('th', {}, t('Когда')),
      )),
    );
    const body = el('tbody');
    for (const action of actions) {
      body.append(
        el('tr', {},
          el('td', {}, el('span', { class: `badge ${badgeForAction(action.action)}`, text: action.action })),
          el('td', { text: action.subject ? `@${action.subject.username}` : '—' }),
          el('td', { class: 'text-sm', text: action.target_type ? `${action.target_type}:${String(action.target_id).slice(0, 8)}` : '—' }),
          el('td', { class: 'text-sm', text: action.moderator ? `@${action.moderator.username}` : t('система') }),
          el('td', { class: 'text-sm clamp-2', text: action.reason }),
          el('td', { class: 'text-sm', title: absoluteTime(action.created_at), text: relativeTime(action.created_at) }),
        ),
      );
    }
    table.append(body);
    list.append(el('div', { class: 'table-wrap' }, table));
  } catch (error) {
    clear(list);
    list.append(errorState({ text: error.message, onRetry: () => actionsSection(host) }));
  }
}

function badgeForAction(action) {
  if (['ban', 'remove', 'suspend'].includes(action)) return 'badge-danger';
  if (['restrict', 'hide', 'warn'].includes(action)) return 'badge-warning';
  if (['restore', 'unban'].includes(action)) return 'badge-success';
  return 'badge-neutral';
}

/* -------------------------------------------------------------------------- */
/* Roles (admin only)                                                          */
/* -------------------------------------------------------------------------- */

async function rolesSection(host) {
  if (!store.isAdmin()) {
    host.append(emptyState({ iconName: 'lock', title: t('Раздел доступен только администраторам') }));
    return;
  }
  clear(host);
  host.append(alert({ variant: 'info', text: t('Роль определяет доступ к панели модератора и административным действиям. Назначайте её осознанно.') }));
  const list = el('div', { class: 'panel', style: { boxShadow: 'none', marginTop: 'var(--space-4)' } });
  host.append(list);

  try {
    const users = await api.get('/admin/users?per_page=100');
    for (const target of users) {
      const roleSelect = el('select', { class: 'select', style: { width: 'auto' } });
      for (const [value, label] of [['member', t('Участник')], ['moderator', t('Модератор')], ['admin', t('Администратор')]]) {
        const option = el('option', { value }, label);
        if (target.role === value) option.selected = true;
        roleSelect.append(option);
      }
      roleSelect.addEventListener('change', async () => {
        try {
          await api.post(`/admin/users/${target.public_id}/role`, { role: roleSelect.value });
          toast.success(t('Роль обновлена: {v0}', { v0: target.username }));
        } catch (error) {
          toast.error(error.message);
          rolesSection(host);
        }
      });
      list.append(
        el('div', { class: 'list-row' },
          avatar(target, { size: 'sm', link: false }),
          el('div', { style: { flex: '1' } },
            el('div', { class: 'list-row-title', text: target.display_name || target.username }),
            el('div', { class: 'list-row-sub', text: `@${target.username} · ${target.status}` }),
          ),
          roleSelect,
        ),
      );
    }
  } catch (error) {
    clear(list);
    list.append(errorState({ text: error.message }));
  }
}
