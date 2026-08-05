import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { renderApp } from "@/shared/test/render";
import { THEME_STORAGE_KEY } from "@/shared/theme/theme-helpers";

/**
 * The theme, end to end through the real app.
 *
 * ## The failure this is really about
 *
 * The theme is written in **two** places: the inline script in
 * `index.html`, which runs before React exists so the first paint is not a
 * flash of the wrong colours, and `shared/theme/theme-context.tsx`, which
 * keeps the DOM in step afterwards. Two writers agreeing is a property
 * nothing enforces — if the script reads `"theme"` and the provider writes
 * `"arena64.theme"`, every reload silently reverts to light and the bug
 * looks like "it forgets my setting sometimes".
 *
 * So this asserts the contract between them: the same storage key, the
 * same class name, on the same element.
 */
describe("the theme", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.classList.remove("dark");
  });

  it("persists the chosen mode under the key the pre-paint script reads", async () => {
    const user = userEvent.setup();
    renderApp();

    // `matchMedia` is stubbed to light in `shared/test/setup.ts`, so
    // "system" resolves to light and the class starts absent.
    expect(document.documentElement).not.toHaveClass("dark");

    await user.click(await screen.findByRole("button", { name: "Dark theme" }));

    expect(document.documentElement).toHaveClass("dark");
    expect(document.documentElement.style.colorScheme).toBe("dark");
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    // The current choice is announced, not merely coloured differently —
    // three buttons that look alike to a screen reader are three buttons
    // with no state.
    expect(screen.getByRole("button", { name: "Dark theme" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    // The other writer. If either half of this drifts, the reload path
    // silently stops working and no functional test would see it.
    // Resolved from the Vitest root (`apps/web`) rather than from
    // `import.meta.url`: a relative URL that climbs out of `src/` is not a
    // `file:` URL under Vite's module graph.
    const bootScript = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
    expect(bootScript).toContain(`localStorage.getItem("${THEME_STORAGE_KEY}")`);
    expect(bootScript).toContain('classList.toggle("dark"');
  });
});
