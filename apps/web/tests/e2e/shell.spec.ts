import { expect, test } from "@playwright/test";

/**
 * One end-to-end journey, against the **built** app in a real browser.
 *
 * Everything else in this phase is asserted in jsdom, which is a good
 * simulation of a DOM and no simulation at all of a browser: it does not
 * lay out, it does not paint, it does not run the pre-paint script in
 * `index.html`, and its `Tab` key is `userEvent`'s idea of one. This test
 * exists for the handful of properties that only survive that distinction —
 * the code-split chunks actually load, the shell actually boots, and a
 * keyboard actually reaches the content.
 *
 * Deliberately one journey. Feature journeys belong to the phases that
 * ship features; an e2e suite written before there is anything to journey
 * through is a suite that tests its own fixtures.
 */
test("the shell boots, splits its routes, and is reachable by keyboard", async ({ page }) => {
  const chunkRequests: string[] = [];
  page.on("request", (request) => {
    if (request.resourceType() === "script") chunkRequests.push(request.url());
  });

  await page.goto("/");

  await expect(page.getByRole("heading", { level: 1, name: "Arena64" })).toBeVisible();

  // The home page arrived as its own chunk. If someone replaces the
  // dynamic import with a static one this drops to a single bundle, the
  // app still works, and the first heavy page silently starts costing
  // every visitor.
  expect(chunkRequests.length).toBeGreaterThan(1);

  // WCAG 2.1 §2.4.1. The very first Tab must reach the skip link, and
  // following it must land focus in the content — the one accessibility
  // affordance no component below the layout can provide.
  await page.keyboard.press("Tab");
  const skipLink = page.getByRole("link", { name: "Skip to content" });
  await expect(skipLink).toBeFocused();
  await expect(skipLink).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(page.locator("main")).toBeFocused();

  // An unknown path renders the 404 **at that path** — a redirect would
  // discard the URL the user got wrong along with their ability to see it.
  await page.goto("/no-such-page");
  await expect(
    page.getByRole("heading", { level: 1, name: "This page does not exist" }),
  ).toBeVisible();
  expect(new URL(page.url()).pathname).toBe("/no-such-page");
});
