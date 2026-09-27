/**
 * Validation helpers for client-side forms.
 *
 * These mirror the server rules for immediate feedback, but are never treated
 * as authoritative: every form still submits to the server, which is the only
 * place the rules are actually enforced. Client validation exists to save a
 * round trip, not to be trusted.
 */

import { t } from '../core/i18n.js';

const RULES = {
  required: (value) => (String(value ?? '').trim() ? null : t('Заполните это поле.')),

  username: (value) => {
    const raw = String(value ?? '').trim().toLowerCase();
    if (!raw) return t('Укажите имя пользователя.');
    if (raw.length < 3) return t('Минимум 3 символа.');
    if (raw.length > 32) return t('Максимум 32 символа.');
    if (!/^[a-z0-9](?:[a-z0-9._-]{1,30})[a-z0-9]$/.test(raw)) {
      return t('Допустимы латинские буквы, цифры, точка, дефис и подчёркивание.');
    }
    if (raw.startsWith('.') || raw.startsWith('-') || raw.startsWith('_')) {
      return t('Имя не может начинаться со специального символа.');
    }
    if (/[._-]{2,}/.test(raw)) return t('Избегайте повторяющихся специальных символов.');
    return null;
  },

  email: (value) => {
    const raw = String(value ?? '').trim();
    if (!raw) return t('Укажите адрес электронной почты.');
    if (raw.length > 254) return t('Слишком длинный адрес.');
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(raw)) return t('Некорректный адрес электронной почты.');
    if (raw.includes('..')) return t('Адрес содержит недопустимую последовательность.');
    return null;
  },

  password: (value) => {
    const raw = String(value ?? '');
    if (!raw) return t('Придумайте пароль.');
    if (raw.length < 10) return t('Минимум 10 символов.');
    if (raw.length > 256) return t('Слишком длинный пароль.');
    if (/(.)\1{3,}/.test(raw)) return t('Не повторяйте один символ четыре раза подряд.');
    if (/(?:password|пароль|qwerty|12345|admin|welcome|letmein|iloveyou)/i.test(raw)) {
      return t('Этот пароль слишком распространён.');
    }
    const classes = [/[a-zа-яё]/, /[A-ZА-ЯЁ]/, /\d/, /[^\w\s]/].filter((re) => re.test(raw)).length;
    if (classes < 2) return t('Добавьте цифры, заглавные буквы или знаки.');
    return null;
  },

  maxLength: (limit) => (value) => {
    const length = String(value ?? '').length;
    if (length > limit) return t('Максимум {v0} символов (сейчас {v1}).', {v0: limit, v1: length });
    return null;
  },
};

/** Rough password strength, 0..4. Advisory only. */
export function passwordStrength(value) {
  const raw = String(value ?? '');
  if (!raw) return { score: 0, label: '' };
  let score = 0;
  if (raw.length >= 10) score += 1;
  if (raw.length >= 14) score += 1;
  if (/[a-z]/.test(raw) && /[A-Z]/.test(raw)) score += 1;
  if (/\d/.test(raw)) score += 1;
  if (/[^\w\s]/.test(raw)) score += 1;
  const labels = ['', t('слабый'), t('средний'), t('хороший'), t('надёжный')];
  return { score: Math.min(score, 4), label: labels[Math.min(score, 4)] };
}

export function validateField(name, value, extra = {}) {
  const rule = RULES[name];
  if (typeof rule === 'function') return rule(value, extra);
  if (typeof rule === 'object' && rule !== null) return rule(value, extra);
  return null;
}

/** Validate a whole form, returning a field -> message map (empty when valid). */
export function validateForm(values, schema) {
  const errors = {};
  for (const [field, rules] of Object.entries(schema)) {
    for (const rule of [].concat(rules)) {
      const message = typeof rule === 'string' ? RULES[rule]?.(values[field]) : rule(values[field]);
      if (message) {
        errors[field] = message;
        break;
      }
    }
  }
  return errors;
}

/**
 * Attach live validation to a form.
 * `schema` maps field name -> rule(s). Validation runs on blur, and on input
 * only once a field has already been marked invalid — telling someone their
 * password is too short while they are still typing the first character is
 * noise, not help.
 */
export function bindValidation(form, schema, { onValidChange } = {}) {
  const state = { touched: new Set(), valid: null };

  const fieldOf = (input) => input.closest('.field') || input.parentElement;
  const errorNode = (input) => {
    const wrapper = fieldOf(input);
    return wrapper?.querySelector('.error-text');
  };

  const show = (input, message) => {
    const node = errorNode(input);
    input.classList.toggle('is-invalid', Boolean(message));
    input.setAttribute('aria-invalid', message ? 'true' : 'false');
    if (node) {
      node.textContent = message || '';
      node.classList.toggle('hidden', !message);
    }
  };

  const check = (input) => {
    const name = input.name;
    const rules = schema[name];
    if (!rules) return null;
    const message = validateField(rules, input.value);
    show(input, message);
    return message;
  };

  form.addEventListener(
    'blur',
    (event) => {
      const input = event.target;
      if (!input.name || !schema[input.name]) return;
      state.touched.add(input.name);
      check(input);
      publish();
    },
    true,
  );

  form.addEventListener('input', (event) => {
    const input = event.target;
    if (!input.name || !schema[input.name]) return;
    // Only live-validate a field the user has already left once.
    if (state.touched.has(input.name)) {
      check(input);
      publish();
    }
    clearServerError(input);
  });

  const publish = () => {
    const errors = {};
    for (const name of state.touched) {
      const input = form.elements[name];
      if (!input) continue;
      const message = validateField(schema[name], input.value);
      if (message) errors[name] = message;
    }
    const valid = Object.keys(errors).length === 0;
    state.valid = valid;
    onValidChange?.(valid, errors);
  };

  return {
    isValid: () => {
      for (const name of Object.keys(schema)) {
        state.touched.add(name);
        const input = form.elements[name];
        if (input) check(input);
      }
      return state.valid !== false && Object.keys(collect(schema, form)).length === 0;
    },
    errors: () => collect(schema, form),
    reset: () => {
      state.touched.clear();
      for (const input of form.querySelectorAll('[name]')) show(input, null);
    },
  };
}

function collect(schema, form) {
  const errors = {};
  for (const [name, rules] of Object.entries(schema)) {
    const input = form.elements[name];
    if (!input) continue;
    const message = validateField(rules, input.value);
    if (message) errors[name] = message;
  }
  return errors;
}

/** Clear a server-reported field error as soon as the user edits the field. */
export function clearServerError(input) {
  const wrapper = input.closest('.field') || input.parentElement;
  const node = wrapper?.querySelector('.error-text');
  if (node && !input.classList.contains('is-invalid')) {
    node.classList.add('hidden');
  }
  const alert = form_of(input)?.querySelector('.field-error');
  if (alert && !form_of(input).querySelector('.is-invalid')) alert.remove();
}

function form_of(input) {
  return input.closest('form') || document;
}

/** Render a 422 field-error map onto a form. */
export function applyServerErrors(form, fields) {
  if (!fields) return;
  let first = null;
  for (const [name, message] of Object.entries(fields)) {
    const input = form.elements[name];
    if (!input) continue;
    const wrapper = input.closest('.field') || input.parentElement;
    showError(input, wrapper, message);
    if (!first) first = input;
  }
  first?.focus();
}

export function showError(input, wrapper, message) {
  if (!wrapper) return;
  let node = wrapper.querySelector('.error-text');
  if (!node) {
    node = document.createElement('p');
    node.className = 'error-text';
    node.setAttribute('role', 'alert');
    wrapper.append(node);
  }
  node.textContent = message;
  node.classList.remove('hidden');
  input.classList.add('is-invalid');
  input.setAttribute('aria-invalid', 'true');
}

export const validators = RULES;
