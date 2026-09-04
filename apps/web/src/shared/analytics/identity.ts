/**
 * The browser's analytics identity — A64-027.1 §9, A64-027.2 §64.
 *
 * Two values, both opaque, neither an authentication of anything:
 *
 *     anonymous_id   this browser, for 30 days (D4)
 *     session_id     this tab, for as long as it is open
 *
 * ## `localStorage`, not a cookie
 *
 * A cookie would be sent on every request to the API for no purpose, and
 * it is the word that makes a consent conversation about banners rather
 * than about what is actually collected. This value never leaves the
 * browser except in an analytics request body, is first-party, and is used
 * for nothing cross-site.
 *
 * ## Why it expires
 *
 * D4: thirty days. Long enough for the acquisition funnel — a person who
 * lands and registers does it in one session, and the window exists for
 * the one who comes back a week later — and short enough that the value is
 * not a durable tracker. The expiry is stored beside the id and checked on
 * read, so it is testable with an injected clock rather than by waiting.
 *
 * ## Rotation on sign-out
 *
 * A new id, because keeping it would attach the next person on a shared
 * computer to the previous one's visit.
 *
 * ## Every read is guarded
 *
 * `localStorage` throws in a private window with site data blocked, and
 * an analytics identity is never worth an exception on a page load. A
 * browser that cannot store one gets a fresh id per page, which undercounts
 * — the documented cost of behavioural measurement (§36).
 */

const ANONYMOUS_KEY = "arena64.analytics.anonymous";
const SESSION_KEY = "arena64.analytics.session";

/** D4, frozen. */
export const ANONYMOUS_ID_TTL_MS = 30 * 24 * 60 * 60 * 1000;

interface StoredIdentity {
  id: string;
  expiresAt: number;
}

function newId(): string {
  return crypto.randomUUID();
}

function read(key: string): StoredIdentity | null {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return null;
    const parsed: unknown = JSON.parse(raw);
    if (
      typeof parsed === "object" &&
      parsed !== null &&
      typeof (parsed as StoredIdentity).id === "string" &&
      typeof (parsed as StoredIdentity).expiresAt === "number"
    ) {
      return parsed as StoredIdentity;
    }
    return null;
  } catch {
    /* Storage disabled, or a value somebody edited. Either is a fresh id. */
    return null;
  }
}

function write(key: string, identity: StoredIdentity): void {
  try {
    localStorage.setItem(key, JSON.stringify(identity));
  } catch {
    /* See `read`. The id still works for this page. */
  }
}

/**
 * This browser's id, rotating after thirty days.
 *
 * `now` is a parameter so the expiry is testable without waiting a month.
 */
export function anonymousId(now: number = Date.now()): string {
  const stored = read(ANONYMOUS_KEY);
  if (stored !== null && stored.expiresAt > now) return stored.id;

  const fresh = { id: newId(), expiresAt: now + ANONYMOUS_ID_TTL_MS };
  write(ANONYMOUS_KEY, fresh);
  return fresh.id;
}

/**
 * This tab's id. `sessionStorage`, so a second tab is a second visit —
 * which is what a session means here and is not what a security session
 * means (§31). Nothing authenticates with this.
 */
export function sessionId(): string {
  try {
    const existing = sessionStorage.getItem(SESSION_KEY);
    if (existing !== null) return existing;
    const fresh = newId();
    sessionStorage.setItem(SESSION_KEY, fresh);
    return fresh;
  } catch {
    return newId();
  }
}

/**
 * A new browser identity — called on sign-out.
 *
 * The session id goes too: the next person on this computer is a new
 * visit as well as a new browser identity.
 */
export function rotateAnonymousId(now: number = Date.now()): string {
  const fresh = { id: newId(), expiresAt: now + ANONYMOUS_ID_TTL_MS };
  write(ANONYMOUS_KEY, fresh);
  try {
    sessionStorage.removeItem(SESSION_KEY);
  } catch {
    /* See `read`. */
  }
  return fresh.id;
}
