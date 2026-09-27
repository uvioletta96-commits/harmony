/**
 * Post composer.
 *
 * Uploads first, then publishes. Sending bytes separately from the post gives
 * the user immediate visual confirmation for large images, keeps a failed
 * upload from losing the typed text, and matches the server's placeholder-row
 * design (the upload is stored unclaimed; creating the post claims it).
 */

import { t } from '../core/i18n.js';

import api from '../core/api.js';
import { el, clear } from '../core/dom.js';
import { icon } from '../core/icons.js';
import toast from '../core/toast.js';
import { button, setLoading, counter, modal } from '../components/ui.js';
import { readDraft, writeDraft, clearDraft, hasDraft } from '../core/drafts.js';

const MAX_CHARS = 5000;
const MAX_MEDIA = 6;
const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
const MAX_VIDEO_BYTES = 50 * 1024 * 1024;
const ACCEPTED = [
  'image/jpeg', 'image/png', 'image/webp', 'image/gif',
  'video/mp4', 'video/webm', 'video/ogg',
];

/** The byte ceiling for one file, which depends on what kind of file it is. */
function sizeLimit(mimeType) {
  return String(mimeType).startsWith('video/') ? MAX_VIDEO_BYTES : MAX_IMAGE_BYTES;
}

function megabytes(bytes) {
  return Math.round(bytes / (1024 * 1024));
}

export function composer({ onPosted, placeholder = 'Что нового?', autofocus = false, restore = true } = {}) {
  const input = el('textarea', {
    class: 'composer-input',
    rows: '1',
    placeholder,
    maxlength: String(MAX_CHARS),
    'aria-label': 'Текст публикации',
  });

  const charCounter = el('span', { class: 'counter' });
  const fileInput = el('input', {
    type: 'file',
    accept: ACCEPTED.join(','),
    multiple: true,
    class: 'visually-hidden',
    'aria-label': 'Прикрепить фото или видео',
  });
  const attachGrid = el('div', { class: 'attach-grid', hidden: true });
  const errorSlot = el('div', { style: { paddingLeft: '52px' } });

  let attachments = [];
  let visibility = 'public';
  let busy = false;

  /* --- Auto-grow ------------------------------------------------------- */
  const resize = () => {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 320)}px`;
    charCounter.textContent = `${input.value.length} / ${MAX_CHARS}`;
    charCounter.classList.toggle('is-warning', input.value.length > MAX_CHARS * 0.9);
  };
  /* --- Drafts ----------------------------------------------------------- */

  // Restored once, on open. The caller passes `draft: false` when it wants a
  // clean box - which is what "publish an empty post" means.
  if (restore) {
    const saved = readDraft();
    if (saved) {
      input.value = saved.body;
      if (saved.lostAttachments?.length) {
        toast.warning(
          t('Черновик восстановлен без файлов: {v0}', { v0: saved.lostAttachments.join(', ') }),
        );
      } else {
        toast.info(t('Восстановлен черновик'));
      }
      resize();
    }
  }

  let draftTimer = null;
  function scheduleDraft() {
    // Debounced: a fast typist produces an event per keystroke and each one
    // would serialise the whole text to storage.
    clearTimeout(draftTimer);
    draftTimer = setTimeout(() => {
      writeDraft(
        input.value,
        attachments.map((item) => item.file.name),
      );
    }, 400);
  }

  // A file cannot be put back into a file input, so a draft records that one was
  // there rather than pretending the post is complete.
  function dropDraft() {
    clearTimeout(draftTimer);
    clearDraft();
  }

  /* --- Attachments ------------------------------------------------------ */
  const renderAttachments = () => {
    clear(attachGrid);
    attachGrid.hidden = attachments.length === 0;
    updateSubmit();
    attachments.forEach((item, index) => {
      // A video gets a <video> in the tray, not an <img> pointing at an MP4:
      // the browser would show a broken-image glyph and no way to tell which
      // file it was.
      const preview = String(item.file.type).startsWith('video/')
        ? el('video', { class: 'attach-preview', src: item.preview, muted: '', playsinline: '' })
        : el('img', { class: 'attach-preview', src: item.preview, alt: '' });

      attachGrid.append(
        el('div', { class: 'attach-item' },
          preview,
          el('button', {
            class: 'attach-remove',
            type: 'button',
            'aria-label': 'Убрать вложение',
            onclick: () => {
              // Captured before the splice: afterwards `attachments[index]` is
              // the *next* item, so revoking it would blank a preview that is
              // still on screen and leak the one actually removed.
              const [removed] = attachments.splice(index, 1);
              URL.revokeObjectURL(removed.preview);
              renderAttachments();
              scheduleDraft();
            },
          }, icon('close', { size: 12 })),
        ),
      );
    });
  };

  fileInput.addEventListener('change', async () => {
    const files = Array.from(fileInput.files || []);
    fileInput.value = '';
    if (!files.length) return;

    const room = MAX_MEDIA - attachments.length;
    if (room <= 0) {
      toast.warning(t('Можно прикрепить не более {v0} файлов', { v0: MAX_MEDIA }));
      return;
    }

    for (const file of files.slice(0, room)) {
      if (!ACCEPTED.includes(file.type)) {
        toast.error(t('«{v0}» — неподдерживаемый формат', { v0: file.name }));
        continue;
      }
      // A video and a photo are not interchangeable, so the ceiling is chosen
      // per file rather than as one number that is wrong for half of them.
      const limit = sizeLimit(file.type);
      if (file.size > limit) {
        toast.error(t('«{v0}» больше {v1} МБ', {v0: file.name, v1: megabytes(limit) }));
        continue;
      }
      attachments.push({ file, preview: URL.createObjectURL(file), storageKey: null, uploading: true });
    }
    renderAttachments();
    await uploadPending();
  });

  async function uploadPending() {
    const pending = attachments.filter((item) => item.uploading);
    if (!pending.length) return;
    for (const item of pending) {
      const body = new FormData();
      body.append('file', item.file);
      try {
        const result = await api.upload('/uploads/images', body);
        item.storageKey = result.files[0].storage_key;
        item.uploading = false;
        item.alt = result.files[0].alt_text;
      } catch (error) {
        attachments = attachments.filter((entry) => entry !== item);
        URL.revokeObjectURL(item.preview);
        renderAttachments();
        toast.error(error.message);
      }
    }
  }

  /* --- Submit ----------------------------------------------------------- */
  const submit = async () => {
    if (busy) return;
    const text = input.value.trim();
    if (!text && attachments.length === 0) return;

    busy = true;
    setLoading(submitButton, true);
    clear(errorSlot);

    try {
      await uploadPending();

      const body = {
        body: text,
        visibility,
        media: attachments.map((item) => ({ storage_key: item.storageKey, alt_text: item.alt || '' })),
      };
      const result = await api.post('/posts', body);

      input.value = '';
      resize();
      for (const item of attachments) URL.revokeObjectURL(item.preview);
      attachments = [];
      renderAttachments();
      dropDraft();

      onPosted?.(result.post);
      if (result.status === 'under_review') {
        toast.info('Публикация отправлена на проверку модератора');
      }
    } catch (error) {
      const node = el('div', { class: 'field-error' },
        icon('alert', { class: 'icon' }),
        el('span', { text: error.message }),
      );
      errorSlot.append(node);
      if (error.code === 'content_blocked') {
        toast.warning(error.message);
      } else if (error.status !== 422) {
        toast.error(error.message);
      }
    } finally {
      busy = false;
      setLoading(submitButton, false);
      updateSubmit();
    }
  };

  const submitButton = button('Опубликовать', { variant: 'primary', size: 'sm', onClick: submit, disabled: true });

  /**
   * Publishing needs a caption, a file, or both - and the button has to be told
   * so every time that changes.
   *
   * It used to be recomputed on the textarea's `input` event only, so attaching
   * a photo to an empty caption left the button disabled for good: the tray
   * filled in, the picture previewed, and there was no way to send it. Both
   * routes into the composer's content have to call this.
   */
  function updateSubmit() {
    const hasContent = input.value.trim().length > 0 || attachments.length > 0;
    submitButton.disabled = !hasContent || busy;
  }

  input.addEventListener('input', () => {
    resize();
    updateSubmit();
    scheduleDraft();
  });

  const visibilitySelect = el('select', {
    class: 'select',
    style: { width: 'auto', height: '32px', padding: '0 28px 0 10px', fontSize: 'var(--text-sm)', borderRadius: 'var(--radius-sm)' },
    'aria-label': 'Видимость публикации',
  });
  for (const [value, text] of [['public', 'Публично'], ['followers', 'Подписчикам'], ['private', 'Только мне']]) {
    visibilitySelect.append(el('option', { value }, text));
  }
  visibilitySelect.addEventListener('change', () => {
    visibility = visibilitySelect.value;
  });

  const node = el('div', { class: 'composer' },
    el('div', { class: 'composer-row' },
      el('div', { class: 'avatar avatar-md', dataset: { color: 'sand' } },
        el('span', { text: window.__harmonyUserInitials || '' }),
      ),
      input,
    ),
    attachGrid,
    errorSlot,
    el('div', { class: 'composer-toolbar' },
      el('div', { class: 'composer-tools' },
        el('button', {
          class: 'icon-btn',
          type: 'button',
          'aria-label': 'Прикрепить изображение',
          title: 'Изображение',
          onclick: () => fileInput.click(),
        }, icon('image', { size: 18 })),
        el('button', {
          class: 'icon-btn',
          type: 'button',
          'aria-label': 'Добавить эмодзи',
          title: 'Эмодзи',
          onclick: () => insertEmoji(input),
        }, icon('smile', { size: 18 })),
        visibilitySelect,
        charCounter,
      ),
      el('div', { style: { display: 'flex', gap: 'var(--space-2)' } },
        // Grouped so the pair travels together when the toolbar wraps, instead
        // of the publish button wrapping away from its own label.
        el('div', { class: 'composer-actions' },
          button('Отмена', {
            variant: 'ghost',
            size: 'sm',
            onClick: () => {
              input.value = '';
              resize();
              for (const item of attachments) URL.revokeObjectURL(item.preview);
              attachments = [];
              renderAttachments();
              updateSubmit();
              dropDraft();
            },
          }),
          submitButton,
        ),
      ),
    ),
    fileInput,
  );

  // Keyboard: Enter publishes, Shift+Enter adds a newline.
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submit();
    }
  });

  if (autofocus) requestAnimationFrame(() => input.focus());
  resize();
  return node;
}

/**
 * Open the composer in a dialog.
 *
 * The composer used to live at the top of the feed, which meant the feed opened
 * with a form in it rather than with posts, and posting from anywhere else meant
 * navigating to the feed first. The "+" in the top bar and the button on a
 * phone both land here instead, so writing is one action from wherever you are.
 *
 * The new post is announced on `document` rather than handed back to a caller:
 * the dialog is opened from the shell, not from the feed, and the feed is not
 * always on screen. A plain event keeps the two apart without either importing
 * the other - `pages/feed.js` listens only while it is mounted.
 */
export function openComposer() {
  const host = el('div', { class: 'composer-host' });
  const { close } = modal({ title: t('Новая публикация'), body: host, wide: true });

  host.append(
    composer({
      autofocus: true,
      onPosted: (post) => {
        document.dispatchEvent(new CustomEvent('harmony:post-created', { detail: post }));
        close();
      },
    }),
  );

  return close;
}

const EMOJI = ['🙂', '🙏', '✨', '🌿', '☕️', '📖', '🌙', '💭', '🌊', '🕯️', '🎧', '🥐'];

function insertEmoji(input) {
  const picker = el('div', {
    class: 'dropdown-menu dropdown-menu-left',
    style: { display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: '2px', minWidth: '200px' },
  });
  for (const glyph of EMOJI) {
    picker.append(el('button', {
      class: 'dropdown-item',
      type: 'button',
      style: { justifyContent: 'center', fontSize: '18px', padding: '4px' },
      text: glyph,
      onclick: () => {
        const start = input.selectionStart ?? input.value.length;
        const end = input.selectionEnd ?? input.value.length;
        input.value = input.value.slice(0, start) + glyph + input.value.slice(end);
        input.focus();
        input.setSelectionRange(start + glyph.length, start + glyph.length);
        input.dispatchEvent(new Event('input'));
        picker.remove();
      },
    }));
  }
  picker.style.position = 'absolute';
  picker.style.bottom = 'auto';
  picker.style.top = '0';
  document.body.append(picker);

  const dismiss = (event) => {
    if (!picker.contains(event.target)) {
      picker.remove();
      document.removeEventListener('click', dismiss, true);
    }
  };
  setTimeout(() => document.addEventListener('click', dismiss, true), 0);
}

export default composer;
