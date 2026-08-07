import type { NotificationTarget } from "@/features/notifications/api";
import { NOTIFICATION_ROUTES } from "@/shared/config/notification-routes";

/**
 * Where a notification takes you — A64-021.1 §6, §20.
 *
 * ## A closed mapper, and `null` for everything it does not know
 *
 * The backend sends a **target type and one identifier**, never a URL, and
 * this is the only place that turns one into a route. That is the whole
 * safety property: a target the server invents tomorrow cannot navigate
 * anywhere here, and an event-supplied string cannot become a destination —
 * there is no branch that concatenates one into a path.
 *
 * `null` means "render this notification, and do not make it a link". A
 * notification that cannot be navigated from is still worth reading, and a
 * broken link is worse than no link: §6's "unknown future targets must
 * degrade safely" is this return value.
 *
 * ## Why a string rather than a TanStack `to`/`params` pair
 *
 * The row renders one `<Link to={href}>`, and the typed form would need a
 * discriminated union of every route's parameter shape threaded through the
 * component. The href is built here from a `ref` the backend validated, and
 * it is always same-origin and always absolute — external navigation is
 * forbidden in v0.x (§6) and is unreachable because no branch below can
 * produce a scheme.
 */
export function notificationHref(target: NotificationTarget): string | null {
  switch (target.type) {
    case "friend_requests":
      // No identifier: the destination is the viewer's own incoming list.
      //
      // From the shared constants since A64-021.6A: the service worker
      // resolves the same type through the same value, and two copies of a
      // path is a push whose tap opens a different page from the row.
      return NOTIFICATION_ROUTES.friendRequests;
    case "player_profile":
      // `ref` is the actor's username, which is what `/players/$username`
      // takes. Encoded rather than interpolated raw — usernames are
      // validated on the way in, and encoding costs nothing to be certain.
      return target.ref ? `/players/${encodeURIComponent(target.ref)}` : null;
    // A64-021.4. Three destinations, all `ref`-carrying, all encoded for the
    // reason above. A missing `ref` renders a non-navigable row rather than
    // a link to a list page: a notification about *a* tournament that could
    // not name one is malformed, and sending somebody to the lobby instead
    // would hide that.
    case "tournament":
      return target.ref
        ? `${NOTIFICATION_ROUTES.tournaments}/${encodeURIComponent(target.ref)}`
        : null;
    case "live_game":
      return target.ref ? `/games/${encodeURIComponent(target.ref)}` : null;
    case "match_replay":
      // The replay, never the live board — the backend chose which, because
      // whether a game is still being played is a server fact.
      return target.ref ? `/games/${encodeURIComponent(target.ref)}/replay` : null;
    default:
      return null;
  }
}
