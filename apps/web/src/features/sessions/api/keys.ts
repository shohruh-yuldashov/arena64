/**
 * The one cache key this feature owns — A64-030.5C.
 *
 * Nothing here is keyed by player, for the reason `notificationKeys` gives:
 * the endpoint takes no user id — the access token is the user — so there is
 * no id to key on, and `SessionProvider` clears the whole cache on sign-out.
 */
export const sessionKeys = {
  root: ["sessions"] as const,
  all: () => ["sessions", "all"] as const,
};
