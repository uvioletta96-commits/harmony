/**
 * Login, registration, password recovery and email verification.
 *
 * All four flows share the same two-column layout: an editorial aside that
 * states the product's character, and a focused form. The forms submit over
 * `fetch`, so a validation error does not discard what the user already typed.
 */

import { t } from '../core/i18n.js';

import api, { ApiError } from '../core/api.js';
import { el, clear } from '../core/dom.js';
import { brandMark, icon } from '../core/icons.js';
import store, { login as storeLogin } from '../core/store.js';
import toast from '../core/toast.js';
import { router } from '../core/router.js';
import { bindValidation, applyServerErrors, passwordStrength } from '../core/validate.js';
import { button, setLoading, alert } from '../components/ui.js';

const PROMISES = [
  { icon: 'shield', text: 'Приватность по умолчанию: вы решаете, кто видит ваши публикации' },
  { icon: 'sparkle', text: 'Никакой рекламы и сторонних трекеров' },
  { icon: 'clock', text: 'Никакого давления на активность — метрики в ваших руках' },
];

function authPage(form) {
  return el('div', { class: 'auth-page' },
    el('aside', { class: 'auth-aside' },
      brandMark({ size: 56, className: 'auth-aside-mark' }),
      el('h1', { class: 'auth-aside-title' }, 'Разговоры без лишнего шума'),
      el('p', { class: 'auth-aside-text' },
        '«Гармония» — социальная сеть, где ценятся спокойный тон, внимание к деталям и уважение к личному пространству.'),
      el('div', { class: 'auth-aside-points' },
        ...PROMISES.map((item) =>
          el('div', { class: 'auth-aside-point' }, icon(item.icon, { size: 18 }), el('span', { text: item.text })),
        ),
      ),
    ),
    el('main', { class: 'auth-main' },
      el('div', { class: 'auth-form' },
        el('a', { class: 'auth-brand-mobile', href: '/', 'aria-label': 'Гармония' },
          brandMark({ size: 34 }),
          el('span', { class: 'brand-word', text: 'Гармония' }),
        ),
        form,
      ),
    ),
  );
}

function nextUrl() {
  const params = new URLSearchParams(location.search);
  const next = params.get('next');
  // Only allow same-origin relative paths: an open redirect here would let an
  // attacker bounce a freshly authenticated user to a phishing page.
  if (!next || !next.startsWith('/') || next.startsWith('//')) return '/';
  return next;
}

/* -------------------------------------------------------------------------- */
/* Login                                                                       */
/* -------------------------------------------------------------------------- */

export function render() {
  // Hand the redirect back to the router. Navigating from here and continuing
  // would append the login form after the feed and leave the user on a page
  // they had already left.
  if (store.get('currentUser')) {
    return { redirect: nextUrl() };
  }

  const identifier = el('input', {
    class: 'input', type: 'text', name: 'identifier', id: 'f-identifier',
    autocomplete: 'username', required: true, placeholder: 'Имя пользователя или почта',
    'aria-required': 'true',
  });
  const password = el('input', {
    class: 'input', type: 'password', name: 'password', id: 'f-password',
    autocomplete: 'current-password', required: true, placeholder: '••••••••••',
    'aria-required': 'true',
  });
  const errorSlot = el('div');

  const submit = button('Войти', { variant: 'primary', type: 'submit' });

  const form = el('form', { novalidate: true },
    el('div', { class: 'auth-form-head' },
      el('h1', { class: 'auth-form-title', text: 'С возвращением' }),
      el('p', { class: 'auth-form-sub', text: 'Войдите, чтобы продолжить разговор.' }),
    ),
    errorSlot,
    el('div', { class: 'field' },
      el('label', { class: 'label', for: 'f-identifier' }, 'Логин или почта'),
      identifier,
      el('p', { class: 'error-text hidden', role: 'alert' }),
    ),
    el('div', { class: 'field' },
      el('label', { class: 'label', for: 'f-password' }, 'Пароль'),
      password,
      el('p', { class: 'error-text hidden', role: 'alert' }),
    ),
    el('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 'var(--space-5)' } },
      el('a', { class: 'hint', href: '/forgot', text: 'Забыли пароль?' }),
    ),
    submit,
    el('div', { class: 'auth-form-foot' },
      'Ещё нет аккаунта? ',
      el('a', { href: '/register', text: 'Зарегистрироваться' }),
    ),
    el('div', { class: 'auth-links' },
      el('a', { class: 'hint', href: '/terms', text: 'Правила' }),
      el('a', { class: 'hint', href: '/privacy', text: 'Конфиденциальность' }),
    ),
  );

  form.append(submit);

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    clear(errorSlot);
    setLoading(submit, true);
    try {
      await storeLogin(identifier.value.trim(), password.value);
      toast.success('Добро пожаловать');
      router.navigate(nextUrl(), { replace: true });
    } catch (error) {
      if (error instanceof ApiError && error.fields) {
        applyServerErrors(form, error.fields);
      } else {
        errorSlot.append(alert({ variant: 'danger', text: error.message }));
      }
    } finally {
      setLoading(submit, false);
    }
  });

  identifier.focus();
  return { node: authPage(form), wide: true };
}

/* -------------------------------------------------------------------------- */
/* Registration                                                                */
/* -------------------------------------------------------------------------- */

export function renderRegister() {
  if (store.get('currentUser')) return { redirect: nextUrl() };

  const email = el('input', { class: 'input', type: 'email', name: 'email', id: 'f-email', autocomplete: 'email', required: true, placeholder: 'you@example.com' });
  const username = el('input', { class: 'input', type: 'text', name: 'username', id: 'f-username', autocomplete: 'username', required: true, maxlength: '32', placeholder: 'mira' });
  const displayName = el('input', { class: 'input', type: 'text', name: 'display_name', id: 'f-display_name', maxlength: '64', placeholder: 'Мира Соколова', optional: true });
  const password = el('input', { class: 'input', type: 'password', name: 'password', id: 'f-password', autocomplete: 'new-password', required: true, placeholder: '••••••••••' });
  const strength = el('div', { class: 'hint' });
  const consent = el('input', { type: 'checkbox', name: 'consent', id: 'f-consent', required: true });
  const marketing = el('input', { type: 'checkbox', name: 'marketing_consent', id: 'f-marketing' });

  const errorSlot = el('div');
  const submit = button('Создать аккаунт', { variant: 'primary', type: 'submit' });

  password.addEventListener('input', () => {
    const { score, label } = passwordStrength(password.value);
    strength.textContent = password.value ? t('Надёжность: {v0}', { v0: label }) : '';
    strength.style.color = ['', 'var(--danger)', 'var(--warning)', 'var(--text-muted)', 'var(--success)'][score];
  });

  const form = el('form', { novalidate: true },
    el('div', { class: 'auth-form-head' },
      el('h1', { class: 'auth-form-title', text: 'Создать аккаунт' }),
      el('p', { class: 'auth-form-sub', text: 'Пара минут — и можно писать.' }),
    ),
    errorSlot,
    el('div', { class: 'field' },
      el('label', { class: 'label', for: 'f-email' }, 'Электронная почта'),
      email,
      el('p', { class: 'hint', text: 'Нужна для подтверждения и восстановления доступа.' }),
      el('p', { class: 'error-text hidden', role: 'alert' }),
    ),
    el('div', { class: 'field' },
      el('label', { class: 'label', for: 'f-username' }, 'Имя пользователя'),
      username,
      el('p', { class: 'hint', text: 'Латиница, цифры, точка, дефис. Это ваш адрес в сети.' }),
      el('p', { class: 'error-text hidden', role: 'alert' }),
    ),
    el('div', { class: 'field' },
      el('label', { class: 'label', for: 'f-display_name' }, 'Отображаемое имя'),
      displayName,
      el('p', { class: 'hint', text: 'Необязательно. По умолчанию — имя пользователя.' }),
      el('p', { class: 'error-text hidden', role: 'alert' }),
    ),
    el('div', { class: 'field' },
      el('label', { class: 'label', for: 'f-password' }, 'Пароль'),
      password,
      strength,
      el('p', { class: 'error-text hidden', role: 'alert' }),
    ),
    el('div', { class: 'field' },
      el('label', { class: 'checkbox', for: 'f-consent' },
        consent,
        el('span', {},
          el('span', {}, 'Я принимаю ', el('a', { href: '/terms', target: '_blank', text: 'Пользовательское соглашение' }),
            ' и ', el('a', { href: '/privacy', target: '_blank', text: 'Политику конфиденциальности' }), '.'),
        ),
      ),
      el('p', { class: 'error-text hidden', role: 'alert' }),
    ),
    el('div', { class: 'field' },
      el('label', { class: 'checkbox', for: 'f-marketing' },
        marketing,
        el('span', {},
          el('span', {}, 'Хочу получать новости о сообществе'),
          el('span', { class: 'hint', style: { display: 'block' } }, 'Необязательно. Отписаться можно в любой момент.'),
        ),
      ),
    ),
    submit,
    el('div', { class: 'auth-form-foot' },
      'Уже есть аккаунт? ',
      el('a', { href: '/login', text: 'Войти' }),
    ),
  );

  form.append(submit);

  const validation = bindValidation(form, {
    email: 'email',
    username: 'username',
    password: 'password',
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    clear(errorSlot);

    if (!validation.isValid()) {
      errorSlot.append(alert({ variant: 'warning', text: 'Проверьте выделенные поля.' }));
      return;
    }
    if (!consent.checked) {
      errorSlot.append(alert({ variant: 'warning', text: 'Без согласия с правилами мы не сможем создать аккаунт.' }));
      consent.focus();
      return;
    }
    setLoading(submit, true);
    try {
      const result = await api.post('/auth/register', {
        email: email.value.trim(),
        username: username.value.trim(),
        display_name: displayName.value.trim(),
        password: password.value,
        consent: true,
        marketing_consent: marketing.checked,
      });
      toast.info(result.message);

      // A deployment can turn address confirmation off entirely
      // (REQUIRE_EMAIL_VERIFICATION). Then the account is usable immediately
      // and the server has already signed us in, so there is nothing to verify
      // and nowhere to wait. Sending the reader to a "check your email" screen
      // in that state is the one outcome that is wrong in every part.
      if (result.signed_in) {
        // The server set the session cookies in the registration response, so
        // there is nothing to post again - the reader only has to be told who
        // they are now.
        store.set({ currentUser: result.user, authResolved: true });
        router.navigate('/');
        return;
      }

      // The server hands back a ready-made confirmation link when it is not
      // actually able to send mail - a local instance, chiefly. Carrying it
      // through to the next screen is the whole difference between "check your
      // inbox" and a link that works: with mail off there is no inbox, and the
      // reader otherwise has no way forward at all.
      const pending = result.verification_url
        ? `&pending=${encodeURIComponent(result.verification_url)}`
        : '';
      // `sent=0` is not the same as "no token yet": it means the server did not
      // send anything, and the next screen has to say so rather than point at
      // an inbox that will stay empty.
      const sent = result.verification_email_sent ? '1' : '0';
      router.navigate(`/verify?sent=${sent}${pending}`);
    } catch (error) {
      if (error instanceof ApiError && error.fields) {
        applyServerErrors(form, error.fields);
      }
      errorSlot.append(alert({ variant: 'danger', text: error.message }));
    } finally {
      setLoading(submit, false);
    }
  });

  email.focus();
  return { node: authPage(form), wide: true };
}

/* -------------------------------------------------------------------------- */
/* Password recovery                                                           */
/* -------------------------------------------------------------------------- */

export function renderForgot() {
  const email = el('input', { class: 'input', type: 'email', name: 'email', id: 'f-email', autocomplete: 'email', required: true });
  const errorSlot = el('div');
  const submit = button('Отправить ссылку', { variant: 'primary', type: 'submit' });

  const form = el('form', { novalidate: true },
    el('div', { class: 'auth-form-head' },
      el('h1', { class: 'auth-form-title', text: 'Восстановление доступа' }),
      el('p', { class: 'auth-form-sub', text: 'Пришлём ссылку для смены пароля. Она действует час.' }),
    ),
    errorSlot,
    el('div', { class: 'field' },
      el('label', { class: 'label', for: 'f-email' }, 'Электронная почта'),
      email,
      el('p', { class: 'error-text hidden', role: 'alert' }),
    ),
    submit,
    el('div', { class: 'auth-form-foot' }, el('a', { href: '/login', text: 'Вернуться ко входу' })),
  );
  form.append(submit);

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    clear(errorSlot);
    setLoading(submit, true);
    try {
      const result = await api.post('/auth/forgot-password', { email: email.value.trim() });
      toast.success(result.message, { duration: 7000 });
      router.navigate('/login', { replace: true });
    } catch (error) {
      errorSlot.append(alert({ variant: 'danger', text: error.message }));
    } finally {
      setLoading(submit, false);
    }
  });

  email.focus();
  return { node: authPage(form), wide: true };
}

export function renderReset() {
  const token = new URLSearchParams(location.search).get('token') || '';
  const password = el('input', { class: 'input', type: 'password', name: 'password', id: 'f-password', autocomplete: 'new-password', required: true });
  const repeat = el('input', { class: 'input', type: 'password', name: 'repeat', id: 'f-repeat', autocomplete: 'new-password', required: true });
  const errorSlot = el('div');
  const submit = button('Сохранить пароль', { variant: 'primary', type: 'submit', disabled: !token });

  const form = el('form', { novalidate: true },
    el('div', { class: 'auth-form-head' },
      el('h1', { class: 'auth-form-title', text: 'Новый пароль' }),
      el('p', { class: 'auth-form-sub', text: 'Выберите пароль, который нигде не используете.' }),
    ),
    errorSlot,
    !token ? alert({ variant: 'danger', text: 'Ссылка не содержит токена. Запросите новую.' }) : null,
    el('div', { class: 'field' },
      el('label', { class: 'label', for: 'f-password' }, 'Новый пароль'),
      password,
      el('p', { class: 'error-text hidden', role: 'alert' }),
    ),
    el('div', { class: 'field' },
      el('label', { class: 'label', for: 'f-repeat' }, 'Повторите пароль'),
      repeat,
      el('p', { class: 'error-text hidden', role: 'alert' }),
    ),
    submit,
    el('div', { class: 'auth-form-foot' }, el('a', { href: '/login', text: 'Вернуться ко входу' })),
  );
  form.append(submit);

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    clear(errorSlot);
    if (password.value !== repeat.value) {
      errorSlot.append(alert({ variant: 'warning', text: 'Пароли не совпадают.' }));
      return;
    }
    setLoading(submit, true);
    try {
      const result = await api.post('/auth/reset-password', { token, password: password.value });
      toast.success(result.message);
      router.navigate('/login', { replace: true });
    } catch (error) {
      errorSlot.append(alert({ variant: 'danger', text: error.message }));
    } finally {
      setLoading(submit, false);
    }
  });

  if (token) password.focus();
  return { node: authPage(form), wide: true };
}

export function renderVerify() {
  const params = new URLSearchParams(location.search);
  const token = params.get('token') || '';
  const sent = params.get('sent') === '1';
  // `sent=0` arrives after a registration on a server with no mail delivery.
  // It has to be told apart from "no token yet", or the screen claims an email
  // is on its way when nothing was ever sent.
  const notSent = params.get('sent') === '0';
  // A link the server could not email, handed over on arrival.
  const pending = params.get('pending') || '';
  const status = el('div');

  const form = el('div', {},
    el('div', { class: 'auth-form-head' },
      el('h1', { class: 'auth-form-title', text: 'Подтверждение адреса' }),
      // The subtitle has to agree with what is below it. Saying "we sent an
      // email" directly above "this server does not send email" is worse than
      // saying nothing.
      el('p', {
        class: 'auth-form-sub',
        text: pending
          ? 'Осталось подтвердить адрес, чтобы войти.'
          : notSent
            ? 'Письмо не отправлено.'
            : 'Мы отправили письмо со ссылкой для подтверждения.',
      }),
    ),
    status,
  );

  if (pending) {
    // Mail is not being delivered on this instance, so the honest thing is to
    // say so and hand over the link rather than point at an inbox that will
    // stay empty.
    status.append(
      alert({ variant: 'info', text: 'Письма на этом сервере не отправляются. Подтвердите адрес по ссылке ниже.' }),
      el('div', { class: 'auth-dev-link' },
        el('a', {
          class: 'btn btn-primary',
          href: pending,
          text: 'Подтвердить адрес и войти',
        }),
        el('p', { class: 'hint', style: { marginTop: 'var(--space-2)' }, text: 'Ссылка одноразовая и действует ограниченное время.' }),
      ),
    );
  } else if (notSent) {
    // No mail backend: there is nothing to wait for and nothing to resend. Say
    // who has to act and what happens meanwhile, so the account is not simply
    // stuck with no explanation.
    status.append(
      alert({
        variant: 'warning',
        text: 'Этот сервер не настроен на отправку почты, поэтому письмо с подтверждением не ушло. '
          + 'Покажите администратору этот адрес — он подтвердит аккаунт вручную.',
      }),
      el('p', { class: 'hint', style: { marginTop: 'var(--space-3)' }, text: 'Ваш пароль уже сохранён. Как только адрес подтвердят, вы сможете войти.' }),
      el('div', { class: 'auth-form-foot' }, el('a', { href: '/login', text: 'Вернуться ко входу' })),
    );
    return { node: authPage(form), wide: true };
  } else if (sent) {
    status.append(
      alert({ variant: 'info', text: 'Проверьте почту, включая папку «Спам». Ссылка действует ограниченное время.' }),
      el('div', { style: { marginTop: 'var(--space-5)' } },
        button('Отправить письмо ещё раз', {
          variant: 'secondary',
          onClick: async (event) => {
            const email = prompt('Повторить отправку на какой адрес?');
            if (!email) return;
            setLoading(event.currentTarget, true);
            try {
              await api.post('/auth/resend-verification', { email: email.trim() });
              toast.success('Если аккаунт существует, письмо отправлено.');
            } catch (error) {
              toast.error(error.message);
            } finally {
              setLoading(event.currentTarget, false);
            }
          },
        }),
      ),
      el('div', { class: 'auth-form-foot' }, el('a', { href: '/login', text: 'Вернуться ко входу' })),
    );
    return { node: authPage(form), wide: true };
  }

  if (!token) {
    status.append(alert({ variant: 'danger', text: 'Ссылка не содержит токена подтверждения.' }));
    return { node: authPage(form), wide: true };
  }

  status.append(loadingBlock());
  api.post('/auth/verify-email', { token })
    .then((result) => {
      // The endpoint opens the session itself, so there is nothing left for the
      // user to do. Adopting the session here and navigating immediately is the
      // whole difference between "it worked" and "why do I have to log in again".
      if (result.signed_in && result.user) {
        store.set({ currentUser: result.user, authResolved: true });
        toast.success(result.message || 'Аккаунт подтверждён');
        router.navigate('/', { replace: true });
        return;
      }
      clear(status);
      status.append(
        alert({ variant: 'success', text: result.message }),
        el('div', { style: { marginTop: 'var(--space-5)' } },
          button('Войти', { variant: 'primary', onClick: () => router.navigate('/login', { replace: true }) }),
        ),
      );
    })
    .catch((error) => {
      clear(status);
      status.append(
        alert({ variant: 'danger', text: error.message }),
        el('div', { class: 'auth-form-foot' },
          'Ссылка истекла или уже использована. ',
          el('a', { href: '/register', text: 'Зарегистрироваться заново' }),
        ),
      );
    });

  return { node: authPage(form), wide: true };
}

function loadingBlock() {
  return el('div', { class: 'loading-row' }, el('span', { class: 'spinner' }), el('span', { text: 'Подтверждаем…' }));
}
