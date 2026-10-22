/**
 * This feature's cache keys — A64-031.E1.
 *
 * None is keyed by player. `me` is whoever the access token names, and
 * `SessionProvider` removes every cached query on sign-out, so it can never
 * answer for the previous person on a shared device. `providers` is the
 * deployment's. A pending Google sign-in is named by an `HttpOnly` cookie
 * this page cannot read, so the browser is the only key there is.
 */
export const authKeys = {
  me: () => ["auth", "me"] as const,
  providers: () => ["auth", "providers"] as const,
  googlePending: () => ["auth", "google", "pending"] as const,
};
