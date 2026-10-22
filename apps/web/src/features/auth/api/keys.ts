/**
 * This feature's cache keys — A64-031.E1.
 *
 * `me` is keyed by the session's user id, because a refresh can change
 * which account a tab is signed in to without a sign-out — see
 * `model/account.ts`. `providers` is the deployment's. A pending Google
 * sign-in is named by an `HttpOnly` cookie this page cannot read, so the
 * browser is the only key there is.
 */
export const authKeys = {
  me: (userId: string | null) => ["auth", "me", userId] as const,
  providers: () => ["auth", "providers"] as const,
  googlePending: () => ["auth", "google", "pending"] as const,
};
