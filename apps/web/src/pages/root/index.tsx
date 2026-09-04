import { isAuthenticated } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import HomePage from "@/pages/home";
import LandingPage from "@/pages/landing";

/**
 * `/`, which is two pages — A64-026.1 §40.10.
 *
 * A signed-in player gets the product home: their standing, and the four
 * places they go from here. A visitor without an account gets the landing
 * page: what Arena64 is, and a way in.
 *
 * ## Why the choice is here rather than inside either page
 *
 * They share nothing. One is being told what this is; the other is being
 * asked where they want to go. Branching inside a single component would
 * mean a component whose two halves have no line in common, and a reader
 * holding both in their head to follow either.
 *
 * `app/router/routes` makes the matching choice about the *chrome*, for
 * the same reason and one layer up: a page cannot remove a shell it is
 * rendered inside.
 *
 * ## `/` stays open
 *
 * There is no guard here and no redirect. A64-025.3 §2 kept the route open
 * and this preserves that — an anonymous visitor is shown a different page,
 * never sent to `/login`. Deep links elsewhere are unaffected: this route
 * is `/` and nothing else.
 *
 * ## Bootstrapping renders the landing
 *
 * `isAuthenticated` is false while the session is still resolving, so the
 * first frames of a returning player's visit are the landing page. That is
 * deliberate and it is the cheaper of the two mistakes: a signed-in player
 * sees their own home a moment later, and an anonymous visitor — who is the
 * majority of the traffic this route exists for — never sees a flash of
 * product navigation they cannot use.
 */
export default function RootPage() {
  const { state } = useSession();

  return isAuthenticated(state) ? <HomePage /> : <LandingPage />;
}
