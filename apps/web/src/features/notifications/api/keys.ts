/**
 * Every cache key the notification surface owns — A64-021.1 §19.
 *
 * Two entries, and they are deliberately siblings rather than one:
 *
 *     list          the pages, owned by a single `useInfiniteQuery` chain
 *     unreadCount   the badge, its own query with its own refetch policy
 *
 * The badge is separate because it is asked far more often than the list and
 * costs a fraction as much (§10) — sharing a key would make rendering a
 * number refetch a page of notifications, which is exactly what its own
 * endpoint exists to avoid.
 *
 * **The cursor is not in any key.** `useInfiniteQuery` owns the page chain
 * under `list()`, which is the whole reason to use it: a key per cursor
 * would make "load more" a new cache entry each time and leave the earlier
 * pages to be garbage-collected out from under the list.
 *
 * **Nothing here is keyed by player.** The endpoints take no recipient — the
 * access token is the recipient — so there is no id to key on. Signing out
 * is what separates two players' caches, and `SessionProvider` clears the
 * whole cache on sign-out for exactly that reason (§30).
 */
export const notificationKeys = {
  root: ["notifications"] as const,
  list: () => ["notifications", "list"] as const,
  unreadCount: () => ["notifications", "unread-count"] as const,
};
