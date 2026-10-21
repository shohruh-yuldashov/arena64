/**
 * This feature's cache keys — A64-031.E1.
 *
 * Neither is keyed by player: `providers` is the deployment's, and a
 * pending Google sign-in is named by an `HttpOnly` cookie this page cannot
 * read, so the browser is the only key there is.
 */
export const authKeys = {
  providers: () => ["auth", "providers"] as const,
  googlePending: () => ["auth", "google", "pending"] as const,
};
