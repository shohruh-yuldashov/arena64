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

  // A level-one heading, unnamed. `/` was the product shell with an
  // "Arena64" heading when this was written; since A64-026.1 an anonymous
  // visitor gets the landing page, whose `h1` is the hero's sentence — and
  // since A64-026.4 the frame around it is `PublicShell` rather than the
  // page itself. What this test is for is that the shell boots at all, and
  // naming whichever page currently answers `/` is how it broke.
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();

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
    page.getByRole("heading", {
      level: 1,
      name: /page not found|sahifa topilmadi|страница не найдена/i,
    }),
  ).toBeVisible();
  expect(new URL(page.url()).pathname).toBe("/no-such-page");
});

/**
 * The shell reserves the system's area — A64-031.C.
 *
 * **What this can and cannot prove.** Chromium reports no safe-area insets,
 * so `env(safe-area-inset-top, 0px)` resolves to zero here and no assertion
 * in this file can demonstrate the iPhone behaviour the fix is for. Real
 * verification is a Home Screen install on a device, after deploy.
 *
 * What it *can* prove is the half that silently fails: that the utility
 * exists in the shipped stylesheet at all. An unknown Tailwind class
 * generates nothing and throws nothing — the page looks correct on every
 * machine anybody tests on, and the padding is simply absent on the one
 * device that needed it. That is why the token is registered in `@theme`
 * rather than written as an arbitrary value, and this is the check that the
 * registration survived the build.
 *
 * The rest asserts the desktop requirement: an inset of zero must leave the
 * layout exactly as it was.
 */
test("the header reserves the top inset, and reserves nothing when there is none", async ({
  page,
}) => {
  await page.goto("/");

  const header = page.locator("header").first();
  await expect(header).toBeVisible();

  // The rule reached the browser, with `env()` intact rather than compiled
  // away or dropped.
  const declaration = await page.evaluate(() => {
    // Recursive: Tailwind v4 emits its utilities inside `@layer`, so the
    // rules are nested one grouping rule deep and a flat scan finds nothing.
    const find = (rules: CSSRuleList): string | null => {
      for (const rule of Array.from(rules)) {
        if (rule instanceof CSSStyleRule && rule.selectorText === ".pt-safe-top") {
          return rule.style.paddingTop || rule.cssText;
        }
        const nested = (rule as CSSGroupingRule).cssRules as CSSRuleList | undefined;
        if (nested !== undefined) {
          const found = find(nested);
          if (found !== null) return found;
        }
      }
      return null;
    };

    for (const sheet of Array.from(document.styleSheets)) {
      try {
        const found = find(sheet.cssRules);
        if (found !== null) return found;
      } catch {
        continue; // cross-origin, not ours
      }
    }
    return null;
  });
  expect(declaration).toContain("safe-area-inset-top");

  // Two surfaces are positioned *from* the top rather than padded away from
  // it, and padding cannot reach either — A64-031.C review.
  //
  //   the account panel      opens at a fixed offset meant to clear the
  //                          header, and the header is now taller
  //   the drawer's close     is `absolute`, so it is placed from the padding
  //                          edge and the drawer's own padding moves the
  //                          title 35px while moving it 0
  //
  // Both must therefore derive their `top` from the inset. Counted rather
  // than matched by selector, because the generated class names are escaped
  // Tailwind and asserting those would be brittle in a way this is not: if
  // either reverts to a constant, the count drops.
  const positionedFromTheInset = await page.evaluate(() => {
    let found = 0;
    const walk = (rules: CSSRuleList) => {
      for (const rule of Array.from(rules)) {
        if (rule instanceof CSSStyleRule && rule.style.top.includes("safe-area-inset-top")) {
          found += 1;
        }
        const nested = (rule as CSSGroupingRule).cssRules as CSSRuleList | undefined;
        if (nested !== undefined) walk(nested);
      }
    };
    for (const sheet of Array.from(document.styleSheets)) {
      try {
        walk(sheet.cssRules);
      } catch {
        continue;
      }
    }
    return found;
  });
  expect(positionedFromTheInset).toBeGreaterThanOrEqual(2);

  // And the element that owns the top edge is the one carrying it.
  await expect(header).toHaveClass(/pt-safe-top/);

  // Desktop, where the inset is zero: the header still begins at the very top
  // and its controls are where they were. A fix that reserved space nobody
  // asked for would show up here as a strip above the bar.
  expect(await header.evaluate((node) => getComputedStyle(node).paddingTop)).toBe("0px");
  const box = await header.boundingBox();
  expect(box?.y).toBe(0);
});
