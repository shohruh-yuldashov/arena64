import { createBrowserHistory, createRouter, type RouterHistory } from "@tanstack/react-router";

import { routeTree } from "@/app/router/routes";

/**
 * A factory, not a module-level singleton.
 *
 * A router owns a history, and a history is mutable state. One shared
 * instance means test A's navigation is test B's starting URL, and the
 * suite passes or fails by file order — the thing CLAUDE.md §6.7 exists to
 * prevent. `createAppRouter` takes a history so a test can hand it a
 * memory one starting at any path.
 *
 * `App` still defaults to one browser instance, because the running app
 * genuinely has exactly one.
 */
export function createAppRouter(history?: RouterHistory) {
  return createRouter({
    routeTree,
    history: history ?? createBrowserHistory(),
    // 100ms before the pending component appears, so a fast chunk load
    // never flashes a spinner. Below that threshold the spinner is
    // perceived as a stutter rather than as progress.
    defaultPendingMs: 100,
    defaultPendingMinMs: 300,
    // No *default* error component, because the root route names one
    // explicitly and a route error bubbles to the nearest ancestor that
    // has one — see `RouteError` in `routes.tsx`.
    //
    // This used to be `undefined` with a comment saying errors belong to
    // the single boundary in `app/providers`. They did not: the router
    // wraps every route in its own `CatchBoundary`, which catches before
    // anything outside the router can, so with no `errorComponent` a throw
    // rendered TanStack's developer panel and warned in the console
    // (A64-025.12A). A comment that describes an intention rather than the
    // behaviour is worse than none.
    defaultErrorComponent: undefined,
  });
}

export type AppRouter = ReturnType<typeof createAppRouter>;

/**
 * Type-safe `<Link to="...">` everywhere in the app.
 *
 * Without this augmentation `to` is `string` and a typo is a runtime 404;
 * with it, a path that is not in the tree is a compile error.
 */
declare module "@tanstack/react-router" {
  interface Register {
    router: AppRouter;
  }
}
