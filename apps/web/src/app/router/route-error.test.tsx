import {
  createMemoryHistory,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppProviders } from "@/app/providers";
import { rootRoute } from "@/app/router/routes";
import { createTestQueryClient } from "@/shared/test/render";

vi.mock("@/shared/lib/report-error", () => ({ reportError: vi.fn() }));
const { reportError } = await import("@/shared/lib/report-error");

/**
 * A throw inside the router reaches this app's error page — A64-025.12A.
 *
 * ## The failure this covers
 *
 * `app/providers` wraps the whole app in one `ErrorBoundary`, and
 * `createAppRouter` turned the router's own error UI off because "errors
 * belong to the one boundary in `app/providers`". That was an intention,
 * not a behaviour: TanStack Router puts a `CatchBoundary` around every
 * route, and a boundary *inside* the tree catches before one outside it can.
 * So a throw from `AppShell` or from any page never reached the outer
 * boundary — the router warned about the missing `errorComponent` and
 * rendered its own developer panel, with the raw message in red monospace.
 *
 * A user reported exactly that panel. These two tests are what the outer
 * boundary's own tests could never have caught, because they mount the real
 * `rootRoute` rather than a hand-assembled tree.
 *
 * ## Why the route tree is built here
 *
 * The app has no route that throws, and adding one to ship would be a
 * defect. So the **real** `rootRoute` — with its real `errorComponent` — is
 * given one child that throws. Everything under test is production code;
 * only the thing that fails is a fixture.
 */

function renderThrowingRoute(error: Error) {
  const failing = createRoute({
    getParentRoute: () => rootRoute,
    path: "/",
    component: () => {
      throw error;
    },
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([failing]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  return render(
    <AppProviders queryClient={createTestQueryClient()}>
      {/* eslint-disable-next-line @typescript-eslint/no-explicit-any -- the
          fixture tree is not the registered one, and typing it as such would
          mean declaring a second `Register` augmentation for a test. */}
      <RouterProvider router={router as any} />
    </AppProviders>,
  );
}

describe("a throw inside the router", () => {
  beforeEach(() => {
    vi.mocked(reportError).mockClear();
    // React logs a caught error to `console.error` by design. The test is
    // about what the user sees, not about React's noise.
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  it("shows this app's error page rather than the router's developer panel", async () => {
    renderThrowingRoute(new Error("boom"));

    expect(
      await screen.findByRole("heading", { level: 1, name: "Something went wrong" }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Try again" })).toBeVisible();

    // The two halves of CLAUDE.md §9.7: a user gets a sentence they can act
    // on, and never the message. "boom" reaching the screen would mean an
    // internal detail reaching whoever is probing the app.
    expect(screen.queryByText(/boom/)).not.toBeInTheDocument();
  });

  it("reports the error, which nothing was doing before", async () => {
    // `ErrorBoundary.componentDidCatch` reported; it never ran, because the
    // router caught first. A failure visible only in the component it
    // happened in is the silent failure CLAUDE.md §2.7 forbids.
    const error = new Error("boom");
    renderThrowingRoute(error);

    await screen.findByRole("heading", { level: 1, name: "Something went wrong" });
    expect(reportError).toHaveBeenCalledWith(error, { scope: "router" });
  });

  it("survives a failure in the translation context itself", async () => {
    // The throw the user actually hit was `useTranslation` outside its
    // provider. An error page that needs the context that just failed
    // renders a second throw instead of a message — which is why
    // `UnexpectedErrorPage` carries hardcoded English and why that reads
    // like an oversight everywhere except here.
    renderThrowingRoute(new Error("useTranslation must be used inside an I18nProvider."));

    expect(
      await screen.findByRole("heading", { level: 1, name: "Something went wrong" }),
    ).toBeVisible();
  });
});
