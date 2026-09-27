/**
 * Unsent posts, kept in this browser.
 *
 * The obvious place for a draft is the server, and the obvious moment to send
 * it is when you press "publish" - which is the moment it is too late. The
 * case that matters is the session ending while the text is still in the box: a
 * logout, an expired token, a closed laptop. There is no session then, so a
 * server-side draft cannot be written *or* read, and the post is gone.
 *
 * So drafts live in `localStorage`, which is available exactly when the account
 * is not. The trade is stated plainly rather than hidden: a draft belongs to the
 * browser, not the account, so it does not follow you to another device and it
 * is readable by anyone with access to this browser's storage. For half-written
 * text that is the right way round - the alternative is losing it.
 *
 * Storage is keyed by author so two accounts on one machine do not read each
 * other's half-written posts, and a signed-out visitor gets their own key
 * rather than the previous account's.
 */

const PREFIX = 'harmony:draft:';

/** localStorage throws in private mode and when the quota is full. */
function storage() {
  try {
    const probe = '__harmony_probe__';
    window.localStorage.setItem(probe, '1');
    window.localStorage.removeItem(probe);
    return window.localStorage;
  } catch {
    return null;
  }
}

let backing = null;
let checked = false;

function store() {
  if (!checked) {
    backing = storage();
    checked = true;
  }
  return backing;
}

/**
 * The author whose drafts this browser is currently holding.
 *
 * Pinned on sign-in and *not* re-read on sign-out, and that asymmetry is the
 * whole point. Logging out is exactly the moment the draft matters most, and a
 * key that followed `currentUser` would move to `anonymous` at that instant:
 * the half-written post would be written under one key and looked for under
 * another, splitting it in two and losing both halves. Pinning keeps the text
 * where it was, so signing back in restores it.
 *
 * A browser that has never been signed in uses `anonymous`, which keeps two
 * people sharing a machine from reading each other's drafts while still giving
 * a signed-out visitor somewhere to put their own.
 */
let author = 'anonymous';

// Bound at first use rather than imported at module scope: `core/store.js`
// imports this module, so a top-level import back would be a cycle.
let authStore = null;

export function bindStore(store) {
  authStore = store;
  if (store.get('currentUser')) author = `u:${store.get('currentUser').public_id}`;
}

function currentAuthor() {
  try {
    const user = authStore?.get('currentUser');
    if (user) author = `u:${user.public_id}`;
  } catch {
    /* keep whatever we had */
  }
  return author;
}

function keyFor() {
  return PREFIX + currentAuthor();
}

/**
 * The draft for the current author, or `null`.
 *
 * @returns {{ body: string, attachments: File[], savedAt: number } | null}
 */
export function readDraft() {
  const backing_store = store();
  if (!backing_store) return null;
  try {
    const raw = backing_store.getItem(keyFor());
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed.body !== 'string' || !parsed.body.trim()) return null;
    return {
      body: parsed.body,
      // Files do not survive JSON, so an attachment cannot be restored. A draft
      // that had a photo attached keeps the text and says so, rather than
      // quietly publishing a text-only post the writer did not intend.
      attachments: [],
      lostAttachments: Array.isArray(parsed.attachmentNames) ? parsed.attachmentNames : [],
      savedAt: typeof parsed.savedAt === 'number' ? parsed.savedAt : 0,
    };
  } catch {
    return null;
  }
}

export function writeDraft(body, attachmentNames = []) {
  const backing_store = store();
  if (!backing_store) return;
  const text = (body || '').trim();
  try {
    if (!text) {
      backing_store.removeItem(keyFor());
      return;
    }
    backing_store.setItem(
      keyFor(),
      JSON.stringify({ body, attachmentNames, savedAt: Date.now() }),
    );
  } catch {
    // A full quota is not worth interrupting someone's typing over.
  }
}

export function clearDraft() {
  const backing_store = store();
  if (!backing_store) return;
  try {
    backing_store.removeItem(keyFor());
  } catch {
    /* nothing to do */
  }
}

/** True when a draft exists, for a badge on the compose button. */
export function hasDraft() {
  return readDraft() !== null;
}
