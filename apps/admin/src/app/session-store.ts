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
 */

let token: string | null = null;

export const accessToken = {
  get: (): string | null => token,
  set: (value: string | null): void => {
    token = value;
  },
  clear: (): void => {
    token = null;
  },
};
