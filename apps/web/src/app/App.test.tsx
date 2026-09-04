import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { useQueryClient } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderApp, renderWithProviders } from "@/shared/test/render";
import { useTheme } from "@/shared/theme/theme-context";

/**
 * The shell, and the reachability guard.
 *
 * `apps/api` has `tests/unit/test_reachability.py`, written after two
 * audits found a component that was implemented, tested, wired to nothing,
 * and silently absent. The frontend's version of that failure is a
 * provider: `ThemeProvider` can be perfect and still not be in the tree,
 * and every test that renders it directly will pass while the running app
 * has no theme.
 *
 * So these tests mount the **real** `App` — not a hand-assembled tree —
 * and the third one asserts, twice over, that nothing in the graph is
 * decorative: structurally, that every provider module is named by the
 * composition root, and functionally, that a probe rendered as an ordinary
 * child can consume each context.
 */

function sourceOf(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), "utf8");
}

describe("the application shell", () => {
  it("renders the lazy home route inside the shell's landmarks", async () => {
    renderApp();

    // The route component arrives through a dynamic import, so it is not
    // in the first commit — waiting for it is what proves the lazy
    // boundary resolves rather than hanging on its pending state.
    //
    // A64-026.1 §40: the probe was an `<h1>` reading "Arena64", which the
    // landing page no longer has — a heading repeating the wordmark three
    // elements below it tells a visitor nothing, so the first thing on the
    // page now says what Arena64 *is*. What this test is about is the
    // chunk resolving and the landmarks existing, so it waits for a
    // level-one heading without naming it.
    expect(await screen.findByRole("heading", { level: 1 })).toBeVisible();

    // The landmarks a screen-reader user navigates by, and the skip link
    // that has to be first. A layout is the only place these can exist.
    expect(screen.getByRole("main")).toBeInTheDocument();
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(screen.getByRole("contentinfo")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Skip to content" })).toBeInTheDocument();
  });

  it("renders the not-found page for a path the router does not know", async () => {
    // A path no route claims. It used to be `/tournaments/…`, which
    // A64-020.6 turned into a real route — so the fixture is chosen to be
    // one nothing will plausibly implement rather than one that merely has
    // not been implemented yet.
    renderApp({ path: "/no-such-page-anywhere" });

    expect(
      await screen.findByRole("heading", { level: 1, name: "This page does not exist" }),
    ).toBeVisible();
    // A 404 that lost the way back is a dead end.
    expect(screen.getByRole("link", { name: "Back to the lobby" })).toHaveAttribute(
      "href",
      "/",
    );
  });

  it("wires every provider in the graph — none is written but unmounted", async () => {
    // --- structural: the composition root names each one -----------------
    // A provider absent from these two files is absent from the app,
    // however carefully it was written. This is the assertion that catches
    // a provider deleted from the tree but left in the codebase.
    const composition = sourceOf("./App.tsx") + sourceOf("./providers/index.tsx");
    for (const provider of [
      "ErrorBoundary",
      "ThemeProvider",
      "QueryClientProvider",
      "RouterProvider",
    ]) {
      expect(composition).toContain(provider);
    }

    // --- functional: an ordinary child can consume each context ----------
    // Naming a provider is not the same as being *under* it — a misplaced
    // nesting compiles and passes the check above. `useTheme` throws
    // outside its provider and `useQueryClient` returns undefined outside
    // its own, so a probe that reads both proves the nesting too.
    function Probe() {
      const { mode } = useTheme();
      const client = useQueryClient();
      return (
        <output data-testid="probe">{`${mode}:${client === undefined ? "no-client" : "client"}`}</output>
      );
    }

    renderWithProviders(<Probe />);

    await waitFor(() => {
      expect(screen.getByTestId("probe")).toHaveTextContent("system:client");
    });
  });
});
