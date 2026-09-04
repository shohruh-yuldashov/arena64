import {
  createRootRoute,
  createRoute,
  lazyRouteComponent,
  Outlet,
  useRouterState,
} from "@tanstack/react-router";

import { RequireAnonymous, RequireAuth, RequireVerifiedEmail } from "@/app/router/guards";
import { RouteError } from "@/app/router/route-error";
import { isAuthenticated } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import NotFoundPage from "@/pages/not-found";
import { Spinner } from "@/shared/ui";
import { AppShell } from "@/widgets/app-shell";
import { PublicShell } from "@/widgets/marketing/public-shell";

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
/**
 * The shell, or the landing page's own chrome — A64-026.1 §40.10.
 *
 * `/` serves two audiences and they need different furniture. A signed-in
 * player gets `AppShell`: product navigation, the notification bell, the
 * account menu. A visitor without an account gets a marketing header whose
 * links point at sections rather than at routes they cannot reach — and
 * putting both on one page would give it two headers.
 *
 * So the choice is made here rather than inside the page, because it is a
 * choice about the *layout*, and a page cannot remove a shell it is
 * rendered inside.
 *
 * ## No flash, and that is what the `bootstrapping` branch is for
 *
 * The session resolves asynchronously. Rendering `AppShell` while it is
 * unknown and swapping to the landing a moment later is the flicker §28
 * forbids — a header that appears and is replaced. So while the session is
 * bootstrapping **at `/`**, neither chrome is drawn: the outlet renders on
 * its own, which is a blank page for the few frames a bootstrap takes and
 * is what both destinations have in common.
 *
 * ## `/tournaments` has two audiences too — A64-026.4 §43.5
 *
 * It opened to visitors without an account, and `AppShell` is the wrong
 * frame for one: its navigation is entirely guarded routes, so a person who
 * followed a shared link would be given a header of destinations that all
 * bounce them to sign-in. They get `PublicShell` instead — the landing
 * page's chrome, which leads somewhere they can go.
 *
 * The condition is `status === "anonymous"` rather than `!isAuthenticated`,
 * and the difference is the whole point of that state. `unavailable` means
 * one request failed, not that the player is signed out; swapping a
 * signed-in player's product navigation for a marketing header because a
 * refresh timed out would tell them they had been logged out. `bootstrapping`
 * would be the §28 flicker. Neither is a claim worth making, so while the
 * session is anything but known-absent the shell stays.
 *
 * Every other route keeps the shell unconditionally, including during the
 * bootstrap, because they have only one audience.
 */
function RootLayout() {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const { state } = useSession();

  if (pathname === "/" && !isAuthenticated(state)) {
    // Anonymous, or not yet known. `pages/landing` brings its own shell;
    // `pages/home` is only ever reached authenticated.
    return <Outlet />;
  }

  if (state.status === "anonymous" && isPublicPath(pathname)) {
    return (
      <PublicShell>
        <Outlet />
      </PublicShell>
    );
  }

  return (
    <AppShell>
      <Outlet />
    </AppShell>
  );
}

/**
 * The paths a visitor without an account is meant to read — §43.5.
 *
 * A list rather than "not in the protected set": the protected set is the
 * larger one and grows with every feature, so deriving public from it makes
 * a new route public by forgetting. This way a new route is private until
 * somebody writes it down here.
 *
 * `/players/$username` is deliberately absent. It is open too, but a
 * profile is reached from inside the product far more often than from
 * outside it, and A64-026.3 §42.3 already decided it is not a page this
 * platform advertises — giving it marketing chrome would be advertising it.
 */
function isPublicPath(pathname: string): boolean {
  return pathname === "/tournaments" || pathname.startsWith("/tournaments/");
}

export const rootRoute = createRootRoute({
  component: RootLayout,
  notFoundComponent: () => <NotFoundPage />,
  errorComponent: RouteError,
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
  component: lazyRouteComponent(() => import("@/pages/root")),
});

/** `?next=` — a string here, a *validated destination* only at use. */
function nextSearch(search: Record<string, unknown>): { next?: string } {
  return typeof search.next === "string" ? { next: search.next } : {};
}

/** `?token=` — the one-time credential a mailed link carries. */
function tokenSearch(search: Record<string, unknown>): { token?: string } {
  return typeof search.token === "string" ? { token: search.token } : {};
}

/**
 * `/verify-email`'s search: a mailed link's `?token=`, or a `?next=` the
 * verified guard captured on its way past — A64-021.5H §18.
 *
 * Both optional and neither implied by the other. A person arriving from a
 * link has no destination in mind; one bounced off a product page does, and
 * `safeRedirect` is what stops that destination being an external URL.
 */
function verifyEmailSearch(search: Record<string, unknown>): {
  token?: string;
  next?: string;
} {
  return { ...tokenSearch(search), ...nextSearch(search) };
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
  validateSearch: verifyEmailSearch,
  // **Not** `sessionPage`, and not `protectedPage` — A64-021.5H §17, §19.
  //
  // The page is two things behind one path: a mailed link lands here with a
  // `?token=` and must work for somebody who has never signed in, and the
  // six-digit form needs a session. The route therefore carries no guard
  // and the *page* requires one for the half that needs it.
  //
  // Wrapping it in the verified guard would be a loop; wrapping it in
  // `RequireAuth` would strand a link that arrived in a mail client the
  // person is not signed in on.
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
 * Wraps a lazily-imported page in `RequireAuth`, then `RequireVerifiedEmail`.
 *
 * Written once because every product route needs it identically, and
 * because the mistake it prevents is silent: a settings route added later
 * without the guard renders a page that calls `/profile/me`
 * unauthenticated, gets a `401`, and looks like a loading failure rather
 * than a missing guard.
 *
 * ## Both guards, in this order — A64-021.5H §19
 *
 * The two answer different questions and the order is the answer to which
 * matters first: a signed-out visitor belongs at `/login`, and only once
 * they are signed in does "have you proved this address" become the next
 * question. Reversed, an anonymous visitor would be sent to `/verify-email`
 * to verify an account they do not have.
 *
 * This is **not** the enforcement. Every write behind these pages is
 * refused by the backend for an unverified account (`VerifiedUser`); what
 * the guard adds is that nobody is shown a screen whose every button
 * returns `403`.
 */
function protectedPage(load: () => Promise<{ default: () => React.JSX.Element }>) {
  const Page = lazyRouteComponent(load);
  return function Protected() {
    return (
      <RequireAuth>
        <RequireVerifiedEmail>
          <Page />
        </RequireVerifiedEmail>
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

/**
 * `/settings/notifications` — A64-021.3 §21.
 *
 * Protected like every other settings page, and for the endpoint's reason
 * rather than a preference: the recipient of a preference is the access
 * token, so there is no anonymous form of this screen.
 */
export const settingsNotificationsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings/notifications",
  component: protectedPage(() => import("@/pages/settings-notifications")),
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

/**
 * `/challenges` — friend challenges, both directions. A64-022.5 §2.
 *
 * Protected **and** verified, like every other product route, and the
 * verified half is not decoration here: `POST /challenges` and its accept,
 * decline and cancel siblings all take `VerifiedUser`, so an unverified
 * account would render a page whose every button returns `403`.
 *
 * Lazy like every other route. A player who never challenges anybody never
 * pays for the list, the dialog, or the offer surface it mounts.
 */
export const challengesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/challenges",
  component: protectedPage(() => import("@/pages/challenges")),
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

// --- notifications — A64-021.1 ----------------------------------------------

/**
 * `/notifications` — what happened while the player was away.
 *
 * **Protected**, and the reason is the endpoint rather than a preference:
 * every notification belongs to exactly one recipient, and the recipient is
 * the access token. There is no anonymous form of this page — an unsigned
 * visitor has no notifications, not an empty list of them.
 *
 * Lazy like every other route, so a player who never opens it never pays
 * for the list, the row and the date formatting.
 */
export const notificationsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/notifications",
  component: protectedPage(() => import("@/pages/notifications")),
});

// --- tournaments — A64-020.6 ------------------------------------------------

/**
 * `/tournaments` — the lobby, **open** since A64-026.4 §43.
 *
 * It was protected, and the guard was honest at the time: every handler on
 * the tournament router took `CurrentUser`, so an anonymous visitor would
 * have rendered a page whose every request took a `401` — an outage, not a
 * sign-in prompt. §3's rule was and is to follow the backend's visibility.
 *
 * The backend moved. The three reads now take an *optional* viewer and
 * answer without one, hiding only `DRAFT`. So does this route. The rule did
 * not change; the thing it points at did.
 */
export const tournamentsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tournaments",
  component: lazyRouteComponent(() => import("@/pages/tournaments")),
});

/**
 * `/tournaments/$tournamentId` — one tournament, its bracket and its result.
 *
 * Open for the lobby's reason, and **openness is not authorization**: a
 * hand-typed id gets the same `404` here as anywhere else, and an
 * unpublished tournament gets that `404` too rather than a `403` that would
 * confirm it exists (§43.2). Entering still needs an account, which
 * `RegistrationPanel` asks for rather than this route.
 *
 * This is the URL a player shares. It has to open for whoever it reaches.
 */
export const tournamentRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tournaments/$tournamentId",
  component: lazyRouteComponent(() => import("@/pages/tournament")),
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
  settingsNotificationsRoute,
  settingsSessionsRoute,
  friendsRoute,
  friendRequestsRoute,
  blockedRoute,
  challengesRoute,
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
  notificationsRoute,
]);
