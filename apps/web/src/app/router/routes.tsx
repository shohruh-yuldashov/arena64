import {
  createRootRoute,
  createRoute,
  lazyRouteComponent,
  Outlet,
} from "@tanstack/react-router";

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
 *   1. **The tree is reviewable.** Every route, its layout and its lazy
 *      boundary are in one file a reader can hold in their head, rather
 *      than inferred from filenames plus a generated artefact nobody reads
 *      but which must be kept committed and in step.
 *   2. **No codegen step to forget.** `npm run build` has one pipeline.
 *      A generated route tree is a second one, and the failure mode is a
 *      route that works locally because someone's watcher was running.
 *
 * The trade is that adding a route is an edit here rather than a new file.
 * That is the correct trade at six layers and one route, and it is worth
 * revisiting if the tree ever gets large enough that this file does not
 * fit on a screen — recorded as an open question in `specs/frontend.md`.
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
 * routes is code splitting that works at fifty; retrofitting it once every
 * page is statically imported is a rewrite of this file.
 */
export const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: lazyRouteComponent(() => import("@/pages/home")),
});

export const routeTree = rootRoute.addChildren([indexRoute]);
