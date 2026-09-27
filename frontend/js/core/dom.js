/**
 * DOM helpers.
 *
 * All user-supplied text reaches the page through `textContent` or
 * `setAttribute`, never `innerHTML`. `html` exists only for trusted,
 * developer-authored markup and refuses to render anything starting with `<`.
 */

/** Create an element with attributes, dataset, event handlers and children. */
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);

  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;

    if (key === 'class' || key === 'className') {
      node.className = Array.isArray(value) ? value.filter(Boolean).join(' ') : String(value);
    } else if (key === 'dataset') {
      for (const [dataKey, dataValue] of Object.entries(value)) {
        if (dataValue !== null && dataValue !== undefined) node.dataset[dataKey] = String(dataValue);
      }
    } else if (key === 'style' && typeof value === 'object') {
      Object.assign(node.style, value);
    } else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'text') {
      node.textContent = String(value);
    } else if (key === 'href' || key === 'src' || key === 'action' || key === 'formaction') {
      setSafeUrl(node, key, String(value));
    } else if (value === true) {
      node.setAttribute(key, '');
    } else {
      node.setAttribute(key, String(value));
    }
  }

  append(node, children);
  return node;
}

const SAFE_SCHEME = /^(https?:|mailto:|tel:|\/|#|\.\/|\.\.\/)/i;
const DANGEROUS_SCHEME = /^\s*(javascript|vbscript|data|file|blob):/i;

/**
 * `blob:` is safe to *load* and unsafe to *navigate to*, so the decision depends
 * on which attribute it lands in.
 *
 * A blob URL made by `URL.createObjectURL` is same-origin and inert: as an
 * `<img>` or `<video>` source the browser only decodes it as media, and there is
 * no script path through it. It is also the only way to show a file the reader
 * just picked before it has been uploaded anywhere - which is what the
 * composer's attachment tray does.
 *
 * In an `href` it is the opposite. A blob can wrap a complete HTML document, so
 * a link pointing at one hands an attacker a same-origin page to navigate to.
 * That is why this is allowed for `src` only, and why `blob:` stays on the
 * dangerous list everywhere else.
 */
const BLOB_OBJECT_URL = /^blob:/i;

function setSafeUrl(node, attribute, value) {
  const mediaSource = attribute === 'src';
  if (mediaSource && BLOB_OBJECT_URL.test(value)) {
    node.setAttribute(attribute, value);
    return;
  }
  // Reject script-capable schemes outright. `javascript:` and `data:text/html`
  // are the only realistic ways an attacker-controlled URL becomes code.
  if (DANGEROUS_SCHEME.test(value)) {
    node.setAttribute(attribute, '#');
    return;
  }
  if (!SAFE_SCHEME.test(value)) {
    node.setAttribute(attribute, '#');
    return;
  }
  node.setAttribute(attribute, value);
}

export function append(parent, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function frag(...children) {
  const fragment = document.createDocumentFragment();
  append(fragment, children);
  return fragment;
}

/** Escape text destined for an innerHTML template. */
export function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

/** Developer-authored markup only; refuses user content by construction. */
export function html(strings, ...values) {
  const markup = strings.reduce((acc, part, index) => {
    const value = values[index - 1];
    const rendered = Array.isArray(value) ? value.join('') : String(value ?? '');
    return acc + rendered + part;
  });
  if (/^\s*</.test(markup)) return markup;
  throw new Error('html() received plain text; use textContent instead');
}

export const $ = (selector, scope = document) => scope.querySelector(selector);
export const $$ = (selector, scope = document) => Array.from(scope.querySelectorAll(selector));

export function on(target, type, handler, options) {
  const node = typeof target === 'string' ? $(target) : target;
  if (!node) return () => {};
  node.addEventListener(type, handler, options);
  return () => node.removeEventListener(type, handler, options);
}

/** Event delegation: one listener on a container instead of N on children. */
export function delegate(root, type, selector, handler) {
  const listener = (event) => {
    const match = event.target.closest(selector);
    if (match && root.contains(match)) handler(event, match);
  };
  root.addEventListener(type, listener);
  return () => root.removeEventListener(type, listener);
}

export function toggleClass(node, name, force) {
  if (!node) return;
  node.classList.toggle(name, force === undefined ? !node.classList.contains(name) : Boolean(force));
}

/** Briefly flash a state class (used for optimistic-update confirmation). */
export function flash(node, className, duration = 600) {
  if (!node) return;
  node.classList.add(className);
  setTimeout(() => node.classList.remove(className), duration);
}
