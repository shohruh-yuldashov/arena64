import {
  createRootRoute,
  createRoute,
  lazyRouteComponent,
  Outlet,
} from "@tanstack/react-router";

import { RequireAnonymous } from "@/app/router/guards";
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
 * That is the correct trade at seven routes, and it is worth revisiting if
 * this file ever stops fitting on a screen — `specs/frontend.md` OQ-3.
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

export const routeTree = rootRoute.addChildren([
  indexRoute,
  loginRoute,
  registerRoute,
  verifyEmailRoute,
  forgotPasswordRoute,
  resetPasswordRoute,
]);
