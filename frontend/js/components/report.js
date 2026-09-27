/**
 * Report dialog.
 *
 * Submitting a report is deliberately low-friction: one tap opens the dialog,
 * one tap on a reason sends it. The dialog never reveals what action was taken
 * or whether other people reported the same thing — that information is what
 * makes mass-reporting attacks work, and telling a reporter "3 people already
 * reported this" actively encourages them.
 */

import { t } from '../core/i18n.js';

import api from '../core/api.js';
import { el } from '../core/dom.js';
import toast from '../core/toast.js';
import { modal } from './ui.js';

const REASONS = [
  { value: 'spam', label: t('Спам или реклама'), hint: t('Навязчивые повторяющиеся публикации') },
  { value: 'harassment', label: t('Оскорбления или травля'), hint: t('Цленаправленное давление на человека') },
  { value: 'hate', label: t('Язык ненависти'), hint: t('Дискриминация по любому признаку') },
  { value: 'violence', label: t('Насилие или угрозы'), hint: t('Призывы к причинению вреда') },
  { value: 'sexual_content', label: t('Неприемлемый контент'), hint: t('Откровенный сексуализированный материал') },
  { value: 'self_harm', label: t('Самоповреждение'), hint: t('Публикации о самоповреждении') },
  { value: 'misinformation', label: t('Достоверность'), hint: t('Заведомо ложная информация') },
  { value: 'impersonation', label: t('Выдача себя за другого'), hint: t('Чужая личность или организация') },
  { value: 'privacy', label: t('Чужие личные данные'), hint: t('Адреса, телефоны, документы') },
  { value: 'copyright', label: t('Нарушение авторских прав'), hint: t('Ваша работа опубликована без разрешения') },
  { value: 'other', label: t('Другое'), hint: t('Опишите ситуацию') },
];

export function reportDialog({ targetType, targetId, targetPreview = null, onDone }) {
  let selected = null;
  let submitting = false;

  const list = el('div', { role: 'radiogroup', 'aria-label': t('Причина жалобы') });
  for (const reason of REASONS) {
    const inputId = `reason-${reason.value}`;
    const input = el('input', { type: 'radio', name: 'report-reason', value: reason.value, id: inputId });
    input.addEventListener('change', () => {
      selected = reason.value;
      setSubmitEnabled(true);
    });
    list.append(
      el('label', { class: 'switch', for: inputId, style: { padding: 'var(--space-2) 0' } },
        input,
        el('span', { class: 'track' }),
        el('span', { class: 'switch-text' },
          el('span', { class: 'switch-label', text: reason.label }),
          el('span', { class: 'switch-hint', text: reason.hint }),
        ),
      ),
    );
  }

  const details = el('textarea', {
    class: 'textarea',
    rows: '3',
    maxlength: '1000',
    placeholder: t('Добавьте контекст, если это поможет разобраться (необязательно)'),
    'aria-label': t('Подробности жалобы'),
  });

  const body = el('div', {},
    targetPreview ? el('blockquote', { class: 'report-content', style: { marginBottom: 'var(--space-4)' } }, targetPreview) : null,
    list,
    el('div', { class: 'field', style: { marginTop: 'var(--space-4)', marginBottom: '0' } },
      el('label', { class: 'label' }, t('Подробности')),
      details,
    ),
    el('p', { class: 'hint', style: { marginTop: 'var(--space-3)' } },
      t('Мы не сообщаем автору, кто отправил жалобу. Решение принимает модератор, и каждое действие можно обжаловать.'),
    ),
  );

  let confirmButton = null;
  const setSubmitEnabled = (enabled) => {
    if (confirmButton) confirmButton.disabled = !enabled || submitting;
  };

  const dialog = modal({
    title: t('Пожаловаться'),
    body,
    actions: [
      { label: t('Отмена'), variant: 'ghost' },
      {
        label: t('Отправить жалобу'),
        variant: 'primary',
        keepOpen: true,
        onClick: async (_event, close) => {
          if (!selected || submitting) return;
          submitting = true;
          confirmButton?.classList.add('is-loading');
          try {
            await api.post('/reports', {
              target_type: targetType,
              target_id: targetId,
              reason: selected,
              details: details.value.trim(),
            });
            close();
            toast.success(t('Жалоба отправлена. Спасибо — мы её рассмотрим.'));
            onDone?.();
          } catch (error) {
            toast.error(error.message);
          } finally {
            submitting = false;
            confirmButton?.classList.remove('is-loading');
          }
        },
      },
    ],
  });

  const footerButtons = dialog.dialog.querySelectorAll('.modal-footer .btn');
  confirmButton = footerButtons[footerButtons.length - 1];
  setSubmitEnabled(false);

  return dialog;
}

/** Confirmation dialog for destructive actions. Resolves to a boolean. */
export function confirmDialog({ title, message, confirmLabel = t('Подтвердить'), variant = 'danger' }) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };

    const dialog = modal({
      title,
      body: el('p', {
        style: { fontSize: 'var(--text-base)', lineHeight: 'var(--leading-relaxed)', color: 'var(--text-secondary)' },
        text: message,
      }),
      actions: [
        { label: t('Отмена'), variant: 'ghost', onClick: () => finish(false) },
        { label: confirmLabel, variant, onClick: () => finish(true) },
      ],
      onClose: () => finish(false),
    });

    // Focus the confirm action: the keyboard user should be able to say yes.
    requestAnimationFrame(() => {
      const buttons = dialog.dialog.querySelectorAll('.modal-footer .btn');
      buttons[buttons.length - 1]?.focus();
    });
  });
}

export { REASONS };
