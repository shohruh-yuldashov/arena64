import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/shared/test/render";

vi.mock("@/shared/lib/report-error", () => ({ reportError: vi.fn() }));

// Imported after the mock so the boundary and this test see the same spy.
const { reportError } = await import("@/shared/lib/report-error");

/**
 * The error boundary, wired the way the app wires it.
 *
 * Two properties, and only the pair is worth anything:
 *
 *   - the user sees a page rather than a blank document, **and**
 *   - somebody is told, before that page renders
 *
 * A boundary that renders a friendly page and reports nothing is
 * CLAUDE.md §2.7's silent failure wearing a nicer face. It is also the
 * more likely defect, because the page is the visible half and the report
 * is the half nobody notices missing.
 *
 * `reset` is asserted too: without a way back, a transient failure is
 * permanent until a full reload, and a user who hit one loses their place
 * to recover from it.
 */
let shouldThrow = true;

/**
 * The throw is controlled from outside React on purpose. Re-rendering with
 * a different prop would replace the tree *above* the boundary too, which
 * would prove that a fresh mount works — not that `reset` recovers the one
 * that failed.
 */
function Boom() {
  if (shouldThrow) {
    throw new Error("a defect below the boundary");
  }
  return <p>the subtree recovered</p>;
}

describe("the root error boundary", () => {
  it("reports the failure, shows the error page, and recovers on reset", async () => {
    // React logs every caught error to the console by design. Silenced so
    // a passing run is not full of red text that reads like a failure.
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    const user = userEvent.setup();

    shouldThrow = true;
    renderWithProviders(<Boom />);

    expect(
      screen.getByRole("heading", { level: 1, name: "Something went wrong" }),
    ).toBeVisible();
    // `role="alert"` — the page is announced, not silently swapped in.
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(reportError).toHaveBeenCalledTimes(1);
    expect(reportError).toHaveBeenCalledWith(
      expect.objectContaining({ message: "a defect below the boundary" }),
      expect.objectContaining({ scope: "app-root" }),
    );

    // Nothing internal reaches the user — CLAUDE.md §9.7's two audiences.
    expect(screen.queryByText(/a defect below the boundary/)).not.toBeInTheDocument();

    // The child stops throwing, the user clicks the way out, the subtree
    // renders again. A boundary without this is a one-way door.
    shouldThrow = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(screen.getByText("the subtree recovered")).toBeVisible();
    consoleError.mockRestore();
  });
});
