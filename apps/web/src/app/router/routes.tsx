import {
  createRootRoute,
  createRoute,
  lazyRouteComponent,
  Outlet,
} from "@tanstack/react-router";

import { RequireAnonymous, RequireAuth } from "@/app/router/guards";
import NotFoundPage from "@/pages/not-found";
import { Spinner } from "@/shared/ui";
import { AppShell } from "@/widgets/app-shell";

/**
 * The route tree — code-based, not file-based.
 *
 * TanStack Router offers both. File-based generates a `routeTree.gen.ts`
 * from a directory convention; code-based declares the tree here. This app
 * takes the second, for two reasons:
 *
 *   1. **The tree is reviewable.** Every route, its layout, its guard and
 *      its lazy boundary are in one file a reader can hold in their head,
 *      rather than inferred from filenames plus a generated artefact
 *      nobody reads but which must be kept committed and in step.
 *   2. **No codegen step to forget.** `npm run build` has one pipeline.
 *      A generated route tree is a second one, and the failure mode is a
 *      route that works locally because someone's watcher was running.
 *
 * The trade is that adding a route is an edit here rather than a new file.
 * At seventeen routes this file no longer fits on a screen, which is the
 * threshold `specs/frontend.md` OQ-3 named — recorded there as due rather
 * than resolved mid-phase.
 *
 * ## Search parameters are validated, not trusted
 *
 * `next` and `token` arrive from a URL somebody else may have written.
 * Each route's `validateSearch` is the boundary where they become typed —
 * and `next` is validated **again** at use, by `safeRedirect`, because
 * being a string is not the same as being a safe destination.
 */

/**
 * The root: the shell, plus the two things a router owns that no page can.
 *
 * `notFoundComponent` here rather than a literal `/404` route, so **every**
 * unmatched path renders it at whatever depth it was typed — and the
 * address bar keeps the URL that was wrong, which a redirect would discard
 * along with the user's ability to see their own typo.
 */
export const rootRoute = createRootRoute({
  component: () => (
    <AppShell>
      <Outlet />
    </AppShell>
  ),
  notFoundComponent: () => <NotFoundPage />,
  // Shown while a lazily-loaded route component is in flight. Without it a
  // slow connection gets an empty shell that looks like a broken page.
  pendingComponent: () => (
    <div className="flex justify-center py-24">
      <Spinner label="Loading page" />
    </div>
  ),
});

/**
 * `/` — dynamically imported, so the shell and the page are separate
 * chunks from the very first route. Code splitting configured at zero
 * routes is code splitting that works at fifty.
 */
export const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: lazyRouteComponent(() => import("@/pages/home")),
});

/** `?next=` — a string here, a *validated destination* only at use. */
function nextSearch(search: Record<string, unknown>): { next?: string } {
  return typeof search.next === "string" ? { next: search.next } : {};
}

/** `?token=` — the one-time credential a mailed link carries. */
function tokenSearch(search: Record<string, unknown>): { token?: string } {
  return typeof search.token === "string" ? { token: search.token } : {};
}

const LoginPage = lazyRouteComponent(() => import("@/pages/login"));
const RegisterPage = lazyRouteComponent(() => import("@/pages/register"));

export const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  validateSearch: nextSearch,
  // Wrapped rather than guarded in `beforeLoad`: the session is a React
  // context, and `beforeLoad` runs before there is a React tree to read it
  // from. See `guards.tsx`.
  component: function LoginRoute() {
    const { next } = loginRoute.useSearch();
    return (
      <RequireAnonymous next={next}>
        <LoginPage />
      </RequireAnonymous>
    );
  },
});

export const registerRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/register",
  validateSearch: nextSearch,
  component: function RegisterRoute() {
    const { next } = registerRoute.useSearch();
    return (
      <RequireAnonymous next={next}>
        <RegisterPage />
      </RequireAnonymous>
    );
  },
});

/**
 * The three link-landing pages are **not** guarded.
 *
 * A signed-in player can legitimately be verifying a new address or
 * following a reset link they requested from another device, and bouncing
 * them home would strand a one-time token they cannot re-request easily.
 */
export const verifyEmailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/verify-email",
  validateSearch: tokenSearch,
  component: lazyRouteComponent(() => import("@/pages/verify-email")),
});

export const forgotPasswordRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/forgot-password",
  component: lazyRouteComponent(() => import("@/pages/forgot-password")),
});

export const resetPasswordRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/reset-password",
  validateSearch: tokenSearch,
  component: lazyRouteComponent(() => import("@/pages/reset-password")),
});

// --- profile — A64-020.3 ----------------------------------------------------

/**
 * Wraps a lazily-imported page in `RequireAuth`.
 *
 * Written once because four routes need it identically, and because the
 * mistake it prevents is silent: a settings route added later without the
 * guard renders a page that calls `/profile/me` unauthenticated, gets a
 * `401`, and looks like a loading failure rather than a missing guard.
 */
function protectedPage(load: () => Promise<{ default: () => React.JSX.Element }>) {
  const Page = lazyRouteComponent(load);
  return function Protected() {
    return (
      <RequireAuth>
        <Page />
      </RequireAuth>
    );
  };
}

/** `/profile` — **the first real production use of `RequireAuth`.** */
export const profileRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/profile",
  component: protectedPage(() => import("@/pages/profile")),
});

/**
 * `/players/$username` — public, and deliberately unguarded.
 *
 * Anyone may look at a player. The server filters what they see; there is
 * no viewer this route turns away.
 */
export const publicProfileRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/players/$username",
  component: lazyRouteComponent(() => import("@/pages/public-profile")),
});

export const settingsProfileRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings/profile",
  component: protectedPage(() => import("@/pages/settings-profile")),
});

export const settingsPreferencesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings/preferences",
  component: protectedPage(() => import("@/pages/settings-preferences")),
});

export const settingsPrivacyRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings/privacy",
  component: protectedPage(() => import("@/pages/settings-privacy")),
});

export const settingsSessionsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings/sessions",
  component: protectedPage(() => import("@/pages/settings-sessions")),
});

// --- social — A64-020.4 -----------------------------------------------------
// All four are protected: every one of them reads or writes the viewer's own
// social graph, and none has an anonymous form.

export const friendsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/friends",
  component: protectedPage(() => import("@/pages/friends")),
});

export const friendRequestsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/friends/requests",
  component: protectedPage(() => import("@/pages/friend-requests")),
});

export const blockedRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/friends/blocked",
  component: protectedPage(() => import("@/pages/blocked")),
});

export const searchRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/search",
  component: protectedPage(() => import("@/pages/search")),
});

// --- play — A64-020.5A ------------------------------------------------------
// Both protected, and both for the same reason: a lobby reads and writes the
// viewer's own queue ticket, and a match belongs to its two participants.
// Neither has an anonymous form to fall back to.

/**
 * `/play` — the lobby.
 *
 * The one place a queue ticket is created, and therefore the one place the
 * lobby's polling runs. Lazy like every other route, so the matchmaking
 * feature's chunk is not paid for by a player who only reads profiles.
 */
export const playRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/play",
  component: protectedPage(() => import("@/pages/play")),
});

/**
 * `/games/$matchId` — where acceptance hands off.
 *
 * A64-020.5A shipped the route and a placeholder; A64-020.5B replaced the
 * component with the live board and touched nothing else here — not the
 * path, not the guard, not the navigation in `PlayPage`. That the swap was
 * one line is what registering the route early bought.
 */
export const gameRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/games/$matchId",
  component: protectedPage(() => import("@/pages/game")),
});

/**
 * `/games/$matchId/replay` — one finished game, played back.
 *
 * A64-020.5E. Behind `RequireAuth` because the replay read is
 * authenticated: `GET /matches/{id}/replay` resolves the viewer from the
 * access token and answers `404` for a casual match they did not play.
 *
 * **The guard is not the authorization.** It stops an anonymous visitor
 * reaching a page that would only get a `401`, and nothing more — the
 * backend decides who may see which match, and a hand-typed match id gets
 * the same `404` here as anywhere else (§3, §24).
 *
 * A sibling of `/games/$matchId` rather than a child, deliberately: a
 * replay is not a mode of the live board. It mounts no socket, no engine
 * and no clock, and nesting would invite sharing a layout that owns all
 * three.
 */
export const replayRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/games/$matchId/replay",
  component: protectedPage(() => import("@/pages/replay")),
});

/**
 * `/games/history` — the authenticated player's finished matches.
 *
 * A64-020.5F. No route parameter: whose history this is comes from the
 * session, so there is nothing in the URL to tamper with. A public
 * `/players/$username/games` would be a different surface with a different
 * privacy answer, and nothing links to one yet.
 *
 * Under `/games/` rather than `/profile/history`, because a match belongs
 * to the game surface and this is where `/games/$matchId/replay` already
 * lives — the two are read together.
 */
export const historyRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/games/history",
  component: protectedPage(() => import("@/pages/history")),
});

// --- tournaments — A64-020.6 ------------------------------------------------

/**
 * `/tournaments` — the lobby.
 *
 * **Protected**, and the reason is the backend's actual policy rather than
 * a preference. `specs/tournament` §7 makes tournaments "public" in the
 * sense that *no viewer is narrower than another* — there is no owner
 * check and no friends-only variant — but every route on this platform
 * outside `/health` still sits behind a session, and the tournament router
 * is no exception: each of its handlers takes `CurrentUser`.
 *
 * So an anonymous visitor here would render a page whose every request
 * takes a `401`, which looks like an outage rather than a sign-in prompt.
 * §3's rule is to follow the backend's visibility, and this is it.
 */
export const tournamentsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tournaments",
  component: protectedPage(() => import("@/pages/tournaments")),
});

/**
 * `/tournaments/$tournamentId` — one tournament, its bracket and its result.
 *
 * Guarded for the lobby's reason. **The guard is not the authorization**:
 * it stops an anonymous visitor reaching a page that would only get a
 * `401`, and nothing more — a hand-typed tournament id gets the same `404`
 * here as anywhere else, because a tournament is there for everybody or
 * absent for everybody.
 */
export const tournamentRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tournaments/$tournamentId",
  component: protectedPage(() => import("@/pages/tournament")),
});

export const routeTree = rootRoute.addChildren([
  indexRoute,
  loginRoute,
  registerRoute,
  verifyEmailRoute,
  forgotPasswordRoute,
  resetPasswordRoute,
  profileRoute,
  publicProfileRoute,
  settingsProfileRoute,
  settingsPreferencesRoute,
  settingsPrivacyRoute,
  settingsSessionsRoute,
  friendsRoute,
  friendRequestsRoute,
  blockedRoute,
  searchRoute,
  playRoute,
  gameRoute,
  // **Before** `/games/$matchId`, so the literal wins over the parameter —
  // otherwise "history" would be read as a match id and the page would ask
  // the gateway to join a room called `history`.
  historyRoute,
  replayRoute,
  tournamentsRoute,
  tournamentRoute,
]);
