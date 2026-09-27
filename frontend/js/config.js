/**
 * Runtime configuration.
 *
 * A module rather than an inline ``<script>`` in index.html: the Content
 * Security Policy forbids inline scripts outright, which is the single most
 * effective anti-XSS control the application has. Configuration is a value, and
 * values belong in importable files.
 *
 * Both consumers fall back to the same defaults, so deleting this file's values
 * degrades to a working application rather than a broken one.
 */

export const config = {
  apiBase: '/api/v1',
  socketUrl: null,   // null -> same origin, path /socket.io
  socketPath: '/socket.io',
  appName: 'Гармония',
};

export default config;
