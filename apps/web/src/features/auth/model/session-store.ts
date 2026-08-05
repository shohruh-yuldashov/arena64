import type { SessionState } from "@/entities/session";

/**
 * The one place the access token lives, and the port everything else reads
 * it through.
 *
 * ## Why a store rather than a module-level `let accessToken`
 *
 * The Axios interceptor needs the current token on every request, and React
 * needs to re-render when it changes. A bare module variable satisfies the
 * first and not the second; a `useState` in a provider satisfies the second
 * and cannot be read from an interceptor that is not a component. Two
 * copies is the shape where they diverge — the interceptor keeps sending a
 * token the provider already replaced, and the symptom is an intermittent
 * `401` nobody can reproduce.
 *
 * So there is exactly **one** value, held here, with a subscription
 * mechanism. React reads it through `useSyncExternalStore`; the interceptor
 * reads it directly. Neither owns it.
 *
 * ## Why the token is never persisted
 *
 * It is held in a closure variable and nowhere else. Not `localStorage`,
 * not `sessionStorage`, not a cookie this app writes. Anything a script can
 * read, an injected script can read; the fifteen-minute access token is
 * deliberately cheap to lose, and the thirty-day refresh token lives in an
 * `HttpOnly` cookie the page cannot touch at all.
 *
 * A reload therefore starts with no token, which is correct: the app asks
 * the cookie for a new one (`bootstrap`) rather than trusting something it
 * found lying in storage.
 */
export type SessionListener = (state: SessionState) => void;

export interface SessionStore {
  getState: () => SessionState;
  subscribe: (listener: SessionListener) => () => void;
  set: (state: SessionState) => void;
  /** The interceptor's read. `null` whenever there is no live session. */
  getAccessToken: () => string | null;
}

export function createSessionStore(
  initial: SessionState = { status: "bootstrapping" },
): SessionStore {
  let state = initial;
  const listeners = new Set<SessionListener>();

  return {
    getState: () => state,

    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },

    set(next) {
      // Reference equality is what `useSyncExternalStore` compares, so an
      // identical object would still re-render every subscriber. Skipping
      // the identical *reference* is free and correct; deep-comparing
      // would be neither.
      if (next === state) return;
      state = next;
      for (const listener of listeners) {
        listener(state);
      }
    },

    getAccessToken: () => (state.status === "authenticated" ? state.accessToken : null),
  };
}
