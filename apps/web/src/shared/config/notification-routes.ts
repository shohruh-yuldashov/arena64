/**
 * Where a notification takes you, when the destination has no identifier —
 * A64-021.6A §5.
 *
 * ## Why this file exists
 *
 * Two things resolve a notification to a route and they must not disagree:
 * `features/notifications/model/navigation.ts`, which the in-app list uses,
 * and `pwa/push-presentation.ts`, which the **service worker** uses. The
 * second is compiled into a separate bundle by a separate build, so a shared
 * module is the only way they can be one decision rather than two copies —
 * and a copy that drifts is a push whose text says "friend request" and
 * whose tap opens the notification list.
 *
 * ## Why only the parameterless ones
 *
 * `/players/{username}` and `/tournaments/{id}` are built from a `ref` the
 * backend validated, and only the in-app path has one — a push payload
 * carries a notification id, which names a notification and not a
 * tournament. So the routes here are exactly the ones both consumers can
 * reach: a list page, reachable with nothing but the type.
 *
 * ## Why a leaf module with no imports
 *
 * The service worker's bundle must stay one file with no module syntax
 * (`assertShippableWorker`). A constants module that imported anything would
 * pull that thing into the worker; this one has nothing to pull.
 */

export const NOTIFICATION_ROUTES = {
  /** Incoming friend requests — where a received request is answered. */
  friendRequests: "/friends/requests",
  /** The friend list — where an accepted request lands. */
  friends: "/friends",
  /** The notification list. The destination for anything not otherwise
   *  mapped, and never nothing: a notification that cannot be navigated
   *  from is still worth reading. */
  notifications: "/notifications",
  /** The tournament lobby. A push knows the *type* but not which
   *  tournament, so the lobby is the honest destination. */
  tournaments: "/tournaments",
} as const;
