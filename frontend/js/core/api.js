/**
 * Harmony API client.
 *
 * One place that knows how to talk to the backend. Responsibilities:
 *   - attach the CSRF header to unsafe methods
 *   - unwrap the `{ok, data, error}` envelope into a resolved value
 *   - transparently refresh an expired access token, exactly once
 *   - surface failures as `ApiError` so callers can branch on `error.code`
 *
 * The refresh is deliberately single-flight: if five requests 401 at once they
 * share one refresh call rather than firing five and invalidating each other's
 * rotated tokens.
 */

import { config } from '../config.js';

const BASE = config.apiBase;

export class ApiError extends Error {
  constructor(payload, status) {
    super(payload?.message || 'Произошла ошибка. Попробуйте позже.');
    this.name = 'ApiError';
    this.status = status;
    this.code = payload?.code || 'unknown_error';
    this.fields = payload?.fields || null;
    this.requestId = payload?.request_id || null;
    this.retryAfter = payload?.retry_after || null;
  }
}

let refreshPromise = null;
let onUnauthorized = () => {};

export function setUnauthorizedHandler(handler) {
  onUnauthorized = handler;
}

function csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)harmony_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

const SAFE = new Set(['GET', 'HEAD', 'OPTIONS']);

async function parse(response) {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return { ok: false, error: { code: 'invalid_response', message: 'Сервер вернул некорректный ответ.' } };
  }
}

/** Wait briefly for the CSRF cookie to arrive rather than failing the first request. */
async function ensureCsrf(retries = 2) {
  let token = csrfToken();
  let attempt = 0;
  while (!token && attempt < retries) {
    await new Promise((resolve) => setTimeout(resolve, 60));
    token = csrfToken();
    attempt += 1;
  }
  return token;
}

async function rawRequest(method, path, { body, headers = {}, signal, auth = true, retry = true } = {}) {
  const url = path.startsWith('http') ? path : `${BASE}${path}`;
  const requestHeaders = { Accept: 'application/json', ...headers };

  if (body instanceof FormData) {
    // Do not set Content-Type: the browser must add the multipart boundary.
  } else if (body !== undefined) {
    requestHeaders['Content-Type'] = 'application/json';
  }

  if (auth && !SAFE.has(method)) {
    const token = await ensureCsrf();
    if (token) requestHeaders['X-CSRF-Token'] = token;
  }

  const response = await fetch(url, {
    method,
    credentials: 'same-origin',
    headers: requestHeaders,
    body: body instanceof FormData ? body : body !== undefined ? JSON.stringify(body) : undefined,
    signal,
  });

  if (response.status === 401 && auth && retry && !path.includes('/auth/')) {
    const refreshed = await tryRefresh();
    if (refreshed) return rawRequest(method, path, { body, headers, signal, auth, retry: false });
    onUnauthorized();
  }

  const payload = await parse(response);

  if (!response.ok || payload?.ok === false) {
    const error = new ApiError(payload?.error, response.status);
    if (response.status === 429 && !error.retryAfter) {
      error.retryAfter = Number(response.headers.get('Retry-After')) || 60;
    }
    throw error;
  }

  return unwrap(payload);
}

/**
 * Unwrap the response envelope.
 *
 * Every endpoint answers `{ ok, data, meta }`, but no page should have to know
 * that: they all read `result.user`, `result.liked`, `for (const p of result)`.
 * Unwrapping here keeps the transport detail in one place.
 *
 * `meta` rides along as a non-enumerable `__meta`, so a paged list can stay a
 * plain array — `for…of`, `map` and `JSON.stringify` all behave — while still
 * carrying the cursor.
 */
function unwrap(payload) {
  const data = payload?.data;
  const meta = payload?.meta;
  if (data === undefined || data === null) return data;

  if (meta && typeof data === 'object') {
    Object.defineProperty(data, '__meta', { value: meta, enumerable: false, configurable: true });
  }
  return data;
}

async function tryRefresh() {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    try {
      const token = await ensureCsrf();
      const response = await fetch(`${BASE}/auth/refresh`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json', ...(token ? { 'X-CSRF-Token': token } : {}) },
      });
      return response.ok;
    } catch {
      return false;
    } finally {
      // Release on the next tick so concurrent callers all observe this result.
      setTimeout(() => {
        refreshPromise = null;
      }, 0);
    }
  })();
  return refreshPromise;
}

/** Drop the expired access cookie so a later request does not retry a known-bad token. */
function clearAccessCookie() {
  document.cookie = 'harmony_access=; Path=/; Max-Age=0; SameSite=Lax';
}

const api = {
  get: (path, options) => rawRequest('GET', path, options),
  post: (path, body, options) => rawRequest('POST', path, { body, ...options }),
  patch: (path, body, options) => rawRequest('PATCH', path, { body, ...options }),
  put: (path, body, options) => rawRequest('PUT', path, { body, ...options }),
  delete: (path, body, options) => rawRequest('DELETE', path, { body, ...options }),
  upload: (path, formData, options) => rawRequest('POST', path, { body: formData, ...options }),
  logout: async () => {
    try {
      await rawRequest('POST', '/auth/logout', { body: {} });
    } finally {
      clearAccessCookie();
    }
  },
  clearAccessCookie,
  base: BASE,
};

export default api;
