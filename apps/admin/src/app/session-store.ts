/**
 * The access token, in memory and nowhere else — A64-024.2 §16.
 *
 * A closure variable, mirroring `apps/web`'s session store. Not
 * `localStorage`, not `sessionStorage`, not a cookie this app writes:
 * anything a script can read is something an injected script can take, and
 * the refresh half is already in an `HttpOnly` cookie this app cannot read.
 *
 * **Holding a token is not authority.** The token says who the session is;
 * whether that account may administer anything is `GET /admin/me`'s answer
 * and is re-asked on every protected navigation. Nothing here caches a
 * role, and there is deliberately no `isAdmin` to set.
 *
 * ## Clearing is an event, not just an assignment — A64-027A.5 §19
 *
 * A refresh exchange can be in flight when a session ends: on sign-out, on
 * a refused token, and between two tests. Its result belongs to the session
 * that started it, so ending that session must abandon it — otherwise the
 * next session awaits a promise resolved against the old one. The listener
 * is how `shared/api/client` hears that without either module reaching into
 * the other.
 */

let token: string | null = null;
const onCleared = new Set<() => void>();

export const accessToken = {
  get: (): string | null => token,
  set: (value: string | null): void => {
    token = value;
  },
  clear: (): void => {
    token = null;
    for (const listener of onCleared) listener();
  },
  /** Runs when the session is cleared. Returns an unsubscribe. */
  onClear: (listener: () => void): (() => void) => {
    onCleared.add(listener);
    return () => onCleared.delete(listener);
  },
};
