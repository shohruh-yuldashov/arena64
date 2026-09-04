import { readFileSync } from "node:fs";

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/shared/test/render";
import { Button, ListState, LoadFailure, Notice } from "@/shared/ui";

/**
 * The design system's contracts — A64-025.2 §21.
 *
 * Behaviour and contract, not implementation: nothing here asserts a hex
 * value or a class string. What it asserts is what a caller may rely on and
 * what a player is owed — that the tokens exist in both themes, that a
 * control is large enough to hit, and that a failure is announced
 * assertively while a success is not.
 */

describe("the token system", () => {
  // The theme *is* the stylesheet in Tailwind v4 — there is no config
  // object to assert against — so the file is the contract. Read from the
  // workspace root, which is where vitest runs.
  const css = readFileSync("src/app/styles/globals.css", "utf8");
  const block = (selector: string): string => {
    const start = css.indexOf(selector);
    expect(start, `${selector} is missing`).toBeGreaterThan(-1);
    return css.slice(start, css.indexOf("}", start));
  };

  it.each(["--primary", "--primary-foreground", "--success", "--warning", "--ring"])(
    "defines %s in both themes",
    (token) => {
      // A token defined once is a token that stops working the moment
      // somebody switches theme — which is exactly how a hardcoded
      // `emerald-600` survived in the rating delta until this task.
      expect(block(":root")).toContain(`${token}:`);
      expect(block(".dark")).toContain(`${token}:`);
    },
  );

  it("gives the brand a hue rather than leaving it neutral", () => {
    // The audit's finding was not that the palette was wrong but that every
    // colour in it had chroma zero. `oklch(L C H)` with a non-zero middle
    // component is the difference, and it is what makes the board's
    // last-move tint and the focus ring read as Arena64's.
    for (const scope of [":root", ".dark"]) {
      const primary = /--primary:\s*oklch\(([\d.]+)\s+([\d.]+)\s+([\d.]+)\)/.exec(block(scope));
      expect(primary, `${scope} has no oklch --primary`).not.toBeNull();
      expect(Number(primary?.[2])).toBeGreaterThan(0.05);
    }
  });

  it("exposes the semantic colours to Tailwind", () => {
    // Declaring a variable and forgetting the `@theme inline` mapping gives
    // a token nothing can use: `bg-success` would silently render nothing.
    const theme = css.slice(css.indexOf("@theme inline"));
    for (const utility of ["--color-success", "--color-warning"]) {
      expect(theme).toContain(utility);
    }
  });
});

describe("the button", () => {
  it("meets the touch-target floor without the caller asking", () => {
    // The rule used to be `min-h-11` pasted at 112 call sites. A primitive
    // that needs a class to be usable is a primitive with a footgun.
    render(<Button>Play</Button>);
    expect(screen.getByRole("button")).toHaveClass("min-h-11");
  });

  it("keeps an icon-only control square at that size", () => {
    render(<Button size="icon" aria-label="Menu" />);
    expect(screen.getByRole("button", { name: "Menu" })).toHaveClass("size-11");
  });
});

describe("the notice", () => {
  it("interrupts for a failure and waits its turn for anything else", () => {
    // The tone chooses the role. A success that steals a screen reader
    // mid-sentence is worse than one nobody hears.
    const { rerender } = render(<Notice tone="error">Could not load</Notice>);
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load");

    rerender(<Notice tone="success">Saved</Notice>);
    expect(screen.getByRole("status")).toHaveTextContent("Saved");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("lets a caller state the role when the rule does not fit", () => {
    render(
      <Notice tone="warning" role="alert">
        Your clock is nearly out
      </Notice>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("says the same thing without colour", () => {
    // Every tone must carry words. This fails if a future tone is added
    // that renders only a tint.
    for (const tone of ["info", "success", "warning", "error"] as const) {
      const { unmount } = render(<Notice tone={tone}>A sentence</Notice>);
      expect(screen.getByText("A sentence")).toBeVisible();
      unmount();
    }
  });
});

describe("the list state", () => {
  it("announces loading politely and failure assertively", () => {
    // Two renders rather than a `rerender`: RTL's rerender drops the
    // wrapper, and this component needs the translation provider.
    const loading = renderWithProviders(
      <ListState isPending isError={false} isEmpty={false} emptyTitle="None" onRetry={() => {}}>
        <p>rows</p>
      </ListState>,
    );
    expect(screen.getByRole("status")).toBeInTheDocument();
    loading.unmount();

    renderWithProviders(
      <ListState isPending={false} isError isEmpty={false} emptyTitle="None" onRetry={() => {}}>
        <p>rows</p>
      </ListState>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button")).toBeInTheDocument();
  });

  it("leaves the empty sentence to the caller", () => {
    // The primitive owns the shape; "no tournaments open" is a domain
    // sentence and stays with the feature that knows it.
    renderWithProviders(
      <ListState
        isPending={false}
        isError={false}
        isEmpty
        emptyTitle="No tournaments open"
        emptyHint="Check back later"
        onRetry={() => {}}
      >
        <p>rows</p>
      </ListState>,
    );
    expect(screen.getByRole("heading", { name: "No tournaments open" })).toBeVisible();
    expect(screen.getByText("Check back later")).toBeVisible();
  });

  it("shows the failure even when the list is also empty", () => {
    // The regression this exists for. A caller computes `isEmpty` from
    // `entries.length === 0`, which is **true** while a request is failing
    // — so if empty won, a broken list would render "nothing here yet" and
    // look exactly like a healthy one. That is precisely what the
    // tournament history did, because it had no failure branch at all.
    renderWithProviders(
      <ListState
        isPending={false}
        isError
        isEmpty
        emptyTitle="No tournaments yet"
        errorMessage="Tournament history could not be loaded."
        onRetry={() => {}}
      >
        <p>rows</p>
      </ListState>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Tournament history could not be loaded.",
    );
    expect(screen.queryByText("No tournaments yet")).not.toBeInTheDocument();
  });

  it("announces what is loading, in the caller's words", () => {
    // "Loading tournaments…" is worth more to somebody who cannot see the
    // skeletons than "Loading…", and it is the difference between six
    // surfaces sounding alike and sounding like themselves.
    renderWithProviders(
      <ListState
        isPending
        isError={false}
        isEmpty={false}
        loadingLabel="Loading tournaments…"
        emptyTitle="None"
        onRetry={() => {}}
      >
        <p>rows</p>
      </ListState>,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Loading tournaments…");
  });

  it("previews the shape of the list it is about to show", () => {
    // A tournament card is 96px and a match row is 56px. Three bars of the
    // wrong height are not a preview — the page jumps when the data lands.
    const { container } = renderWithProviders(
      <ListState
        isPending
        isError={false}
        isEmpty={false}
        emptyTitle="None"
        pendingRows={4}
        pendingRowClassName="h-24"
        onRetry={() => {}}
      >
        <p>rows</p>
      </ListState>,
    );
    const bars = container.querySelectorAll('[data-slot="skeleton"]');
    expect(bars).toHaveLength(4);
    expect(bars[0]).toHaveClass("h-24");
  });
});

describe("the load failure", () => {
  it("offers a way to try again and never shows the error itself", () => {
    // A player gets a sentence they can act on; the diagnostic goes to
    // `reportError`. A status code in the interface tells the one person
    // who cannot use it.
    const onRetry = vi.fn();
    renderWithProviders(
      <LoadFailure message="Tournaments could not be loaded." onRetry={onRetry} />,
    );

    const retry = screen.getByRole("button");
    fireEvent.click(retry);
    expect(onRetry).toHaveBeenCalledOnce();
    expect(screen.getByRole("alert")).toHaveTextContent("Tournaments could not be loaded.");
  });

  it("falls back to the generic sentence rather than rendering nothing", () => {
    renderWithProviders(<LoadFailure onRetry={() => {}} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/\S/);
  });
});
