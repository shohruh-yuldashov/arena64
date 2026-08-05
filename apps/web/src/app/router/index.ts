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
    // The router's own error UI is deliberately off: errors belong to the
    // one boundary in `app/providers`, so there is a single place that
    // reports them and a single page a user can be shown.
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
