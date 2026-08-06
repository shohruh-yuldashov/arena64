/**
 * The one cache key this feature owns — A64-021.3.
 *
 * A sibling of `notificationKeys`, deliberately **not** a child of it. The
 * two are invalidated by different things: a preference changes when this
 * player saves the form, a notification list changes when somebody else
 * acts. Nesting them would make every arriving notification refetch the
 * settings screen, and every save refetch the list.
 *
 * Nothing here is keyed by player, for the reason `notificationKeys` gives:
 * the endpoint takes no user id — the access token is the user — so there
 * is no id to key on, and `SessionProvider` clears the whole cache on
 * sign-out.
 */
export const notificationPreferenceKeys = {
  root: ["notification-preferences"] as const,
  all: () => ["notification-preferences", "all"] as const,
};
