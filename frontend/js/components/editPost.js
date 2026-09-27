/**
 * Editing a published post.
 *
 * One dialog for both places a post is shown - the feed and the post page -
 * because an "edit" that behaves differently depending on where you opened it
 * is the kind of inconsistency people stop trusting.
 *
 * Resolves with the updated post, or ``null`` if the reader backed out. Callers
 * swap in the new copy rather than reloading: the edit is already saved, and a
 * reload throws away the scroll position and every comment loaded below.
 */

import { t } from '../core/i18n.js';
import { el, clear } from '../core/dom.js';
import api from '../core/api.js';
import toast from '../core/toast.js';
import { alert, button, modal } from './ui.js';

const MAX_CHARS = 5000;

/**
 * @param {object} post the post as currently rendered
 * @returns {Promise<object|null>}
 */
export function editPostDialog(post) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };

    const area = el('textarea', {
      class: 'composer-input',
      rows: '6',
      maxlength: String(MAX_CHARS),
      'aria-label': t('Текст публикации'),
    });
    area.value = post.body || '';

    const counter = el('span', { class: 'counter' });
    const errorSlot = el('div');

    const resize = () => {
      counter.textContent = `${area.value.length} / ${MAX_CHARS}`;
      counter.classList.toggle('is-warning', area.value.length > MAX_CHARS * 0.9);
      area.style.height = 'auto';
      area.style.height = `${Math.min(area.scrollHeight, 320)}px`;
    };
    area.addEventListener('input', resize);

    async function submit(close) {
      const body = area.value.trim();
      if (!body) {
        clear(errorSlot);
        errorSlot.append(alert({ variant: 'warning', text: t('Пустая публикация не сохранится.') }));
        return;
      }
      clear(errorSlot);
      const action = errorSlot.ownerDocument.querySelector('.modal-footer .btn-primary');
      if (action) {
        action.disabled = true;
        action.textContent = t('Сохраняем…');
      }
      try {
        const result = await api.patch(`/posts/${post.id}`, { body });
        toast.success(t('Публикация обновлена'));
        finish(result.post);
        close();
      } catch (error) {
        // In the dialog rather than a toast: the text is still in the textarea,
        // and "failed" flashing over a form with no visible error explains
        // nothing.
        errorSlot.append(alert({ variant: 'danger', text: error.message }));
        if (action) {
          action.disabled = false;
          action.textContent = t('Сохранить');
        }
      }
    }

    area.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        submit(dialog.close);
      }
    });

    const dialog = modal({
      title: t('Редактировать публикацию'),
      body: el('div', {},
        el('div', { class: 'field' }, area, errorSlot),
        el('div', { class: 'composer-toolbar' },
          el('span', { class: 'hint', text: t('Ctrl + Enter — сохранить') }),
          counter,
        ),
      ),
      actions: [
        { label: t('Отмена'), variant: 'ghost' },
        // keepOpen: the dialog must survive a failed save. modal() otherwise
        // closes first and calls onClick afterwards, which would throw away the
        // text the reader just wrote and then show the error on an empty form.
        { label: t('Сохранить'), variant: 'primary', keepOpen: true, onClick: (_event, close) => submit(close) },
      ],
    });

    resize();
    area.focus();
    area.setSelectionRange(area.value.length, area.value.length);
  });
}
