import type { ErrorComponentProps } from "@tanstack/react-router";
import { useEffect } from "react";

import UnexpectedErrorPage from "@/pages/unexpected-error";
import { reportError } from "@/shared/lib/report-error";

/**
 * A throw anywhere inside the router, as this app's error page — A64-025.12A.
 *
 * ## Why the outer boundary was not enough
 *
 * `app/providers` wraps everything in one `ErrorBoundary` and
 * `createAppRouter` turned the router's own error UI off, on the stated
 * grounds that "errors belong to the one boundary in `app/providers`". That
 * was not true of the running app. TanStack Router puts a `CatchBoundary`
 * around every route, and a boundary **inside** the tree catches before one
 * outside it can — so a throw from `AppShell` or any page never reached
 * `app/providers` at all. What the router did instead was warn
 * ("consider setting an `errorComponent` in your RootRoute!") and render its
 * own developer panel: a bold "Something went wrong!", a "Hide Error"
 * toggle, and the raw message in red monospace.
 *
 * That panel is what a user actually saw. The outer boundary is still
 * correct and still catches what is above the router; this closes the half
 * of the tree it could never reach.
 *
 * ## It reports, then renders
 *
 * The router does not report — `ErrorBoundary.componentDidCatch` did, and
 * that never ran. Reporting here keeps the rule that no failure is only
 * visible in the component it happened in.
 *
 * `useEffect`, not the render body: a route can re-render for reasons that
 * are not a new failure, and reporting from render would send the same
 * error repeatedly.
 *
 * ## It is deliberately translation-free
 *
 * `UnexpectedErrorPage` carries hardcoded English, which reads like an
 * oversight until it is this page. One of the throws it has to survive is
 * `useTranslation` itself — and an error page that needs the context that
 * just failed renders a second throw instead of a message.
 */
export function RouteError({ error, reset }: ErrorComponentProps) {
  useEffect(() => {
    reportError(error, { scope: "router" });
  }, [error]);

  return <UnexpectedErrorPage reset={reset} />;
}
