/**
 * Application bootstrap.
 *
 * Order matters: resolve the session before the first route resolves, so a
 * guard never redirects a signed-in user to the login page on a hard refresh.
 */

import i18n, { t } from './core/i18n.js';

import store, { resolveSession, startCounterPolling } from './core/store.js';
import { router } from './core/router.js';
import { renderShell } from './components/shell.js';
import toast from './core/toast.js';
import realtime from './core/realtime.js';

/* Routes ------------------------------------------------------------------ */

import * as feedPage from './pages/feed.js';
import * as loginPage from './pages/login.js';
import * as registerPage from './pages/register.js';
import * as profilePage from './pages/profile.js';
import * as postPage from './pages/post.js';
import * as searchPage from './pages/search.js';
import * as videosPage from './pages/videos.js';
import * as chatPage from './pages/chat.js';
import * as settingsPage from './pages/settings.js';
import * as adminPage from './pages/admin.js';
import * as notificationsPage from './pages/notifications.js';
import * as legalPage from './pages/legal.js';
import * as notFoundPage from './pages/notfound.js';

const PUBLIC_ROUTES = [
  '/', '/login', '/register', '/forgot', '/reset-password', '/verify',
  '/search', '/videos', '/terms', '/privacy',
];

function isPublic(path) {
  const clean = path.split('?')[0].replace(/\/$/, '') || '/';
  if (PUBLIC_ROUTES.includes(clean)) return true;
  // Profile and post pages are readable by anyone; only writes require auth.
  return clean.startsWith('/u/') || clean.startsWith('/post/');
}

router.define('/', feedPage.render);
router.define('/login', loginPage.render);
router.define('/register', registerPage.render);
router.define('/forgot', loginPage.renderForgot);
router.define('/reset-password', loginPage.renderReset);
router.define('/verify', loginPage.renderVerify);
router.define('/u/:username', profilePage.render);
router.define('/post/:id', postPage.render);
router.define('/search', searchPage.render);
router.define('/videos', videosPage.render);
router.define('/chat', chatPage.render);
router.define('/chat/:id', chatPage.render);
router.define('/notifications', notificationsPage.render);
router.define('/settings', settingsPage.render);
router.define('/settings/:section', settingsPage.render);
router.define('/admin', adminPage.render);
router.define('/admin/:section', adminPage.render);
router.define('/terms', legalPage.renderTerms);
router.define('/privacy', legalPage.renderPrivacy);
router.setNotFoundView(notFoundPage.render);

/** Redirect unauthenticated visitors away from private routes. */
router.setGuard((path) => {
  if (store.get('authResolved') && !store.get('currentUser') && !isPublic(path)) {
    const next = encodeURIComponent(location.search + location.hash);
    return `/login?next=${next}`;
  }
  return true;
});

/* Boot -------------------------------------------------------------------- */

async function boot() {
  // The language has to be settled before the first node is built, because the
  // shell, the routes and every string in them are rendered once. Starting from
  // the browser's own preference is what lets a visitor see their language
  // before they have an account; the account value, once known, wins.
  i18n.init();

  const root = document.getElementById('app');
  root.append(renderShell());

  try {
    await resolveSession();
  } catch (error) {
    // The API is unreachable. Say so plainly instead of showing a broken shell.
    toast.error(t('Сервер недоступен. Проверьте соединение и обновите страницу.'), { duration: 0 });
  }

  // The session carries the reader's own choice. Re-resolving here is a no-op
  // for a signed-out visitor and switches language for one who set it on
  // another device, which is the entire point of storing it on the account.
  const currentUser = store.get('currentUser');
  if (currentUser?.language) i18n.syncWithProfile(currentUser.language);

  if (currentUser) {
    startCounterPolling();
    realtime.connect();
    store.subscribe('notifications', (value) => {
      document.title = value.unread > 0 ? t('({v0}) Гармония', { v0: value.unread }) : t('Гармония · Гармония');
    });
  }

  store.subscribe('logged-out', () => {
    realtime.disconnect();
    location.href = '/login';
  });

  store.subscribe('connection', (value) => {
    if (value === 'offline') toast.warning(t('Нет соединения. Публикации сохранятся локально до восстановления связи.'), { duration: 4000 });
  });

  await router.start();
}

window.addEventListener('error', (event) => {
  console.error('[harmony] uncaught error', event.error || event.message);
});

window.addEventListener('unhandledrejection', (event) => {
  // Network errors are already surfaced by the API layer; swallowing them here
  // avoids a second, duplicate toast for the same failure.
  const reason = event.reason;
  if (reason?.name === 'ApiError') {
    event.preventDefault();
    return;
  }
  console.error('[harmony] unhandled rejection', reason);
});

boot();
