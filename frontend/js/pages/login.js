/** Login page.

Every entry point ``main.js`` registers is re-exported here, not just the login
form. The list used to export only ``login`` and ``default``, while the router
was handed ``loginPage.render`` and ``loginPage.renderForgot`` /
``renderReset`` / ``renderVerify`` - all of them ``undefined``.

The failure mode is quiet and misleading: the route matched, the router found
no loader, logged one warning naming the *URL* rather than the export, and
rendered an empty page. Signing in and registering both looked like a dead
server, and a browser refresh was the only thing that fixed it, because a full
load does not go through the router.

``tools/check_frontend.py`` now resolves every ``router.define`` target, so a
missing re-export fails the build instead of a page.
 */
export {
  render,
  render as login,
  render as default,
  renderForgot,
  renderReset,
  renderVerify,
} from './auth.js';
