import { expect, type Page } from "@playwright/test";

/**
 * Navigating to a page that has actually finished booting — A64-021.2H §16.
 *
 * ## The flake this exists for, and why it is not a product bug
 *
 * Every page load starts with `POST /auth/browser/refresh`. Under the full
 * nine-project suite the preview server occasionally drops that one request,
 * and `SessionProvider` does exactly what A64-020.2 designed it to do: it
 * refuses to claim the visitor is signed out, renders "we could not check
 * your session" and offers Try again.
 *
 * That is correct behaviour. What is *not* correct is a spec that navigates
 * once and asserts immediately against it — the assertion then fails on a
 * screen that is working as specified, and the failure names whatever
 * control was missing rather than the dropped request that caused it.
 *
 * The same cause has now been observed failing four different specs across
 * three phases — `social`, `profile`, `play` and `notifications` — each time
 * reported as a different missing element. One helper, one cause.
 *
 * ## What it does not do
 *
 * It does not weaken anything. It waits for the app to have *booted* and
 * then hands the page back; every assertion a spec made before still runs,
 * unchanged, against a page that has had its chance to load.
 *
 * It does not click Try again either. Retrying the navigation is what a
 * player does, and it exercises the same path a first visit does — pressing
 * the recovery button would test the recovery button instead.
 */

/**
 * What proves the session resolved: the header's sign-out control, which
 * `SessionMenu` renders for an authenticated session and for nothing else.
 *
 * A **positive** signal, deliberately. The first version of this helper
 * waited for the *absence* of "we could not check your session", and an
 * absence is satisfied by a page that has not rendered anything yet — so it
 * returned during the gap between `goto` and the first paint, and the spec
 * failed on the alert that appeared a moment later. Waiting for something to
 * exist cannot be satisfied early.
 */
const SIGNED_IN = /sign out/i;

/**
 * How long a session bootstrap is given, across retries.
 *
 * Sized to what was measured rather than guessed: the whole journey takes
 * ~2 s against an idle preview server, and the full nine-project suite
 * saturates that one server hard enough that a bootstrap has been observed
 * failing for more than thirty seconds. A minute is the observed cost with
 * room, not an attempt to outlast a genuine failure — a session that is
 * really dead never produces this control, so this budget expires and the
 * spec fails, which is the correct outcome.
 */
const BOOT_BUDGET_MS = 60_000;

/**
 * Goes to `path` and returns once the session has resolved **as signed
 * in**, retrying the navigation if the bootstrap request was dropped.
 *
 * For a signed-in journey only; an anonymous page has no such marker and
 * does not have this problem, because it makes no bootstrap request whose
 * failure it must report.
 */
export async function gotoBooted(page: Page, path: string): Promise<void> {
  await expect(async () => {
    await page.goto(path);
    await expect(page.getByRole("button", { name: SIGNED_IN })).toBeVisible({
      timeout: 5_000,
    });
  }).toPass({ timeout: BOOT_BUDGET_MS });
}

/**
 * Reloads and returns once the session has resolved again.
 *
 * Separate from `gotoBooted` because a reload is a different claim: a spec
 * calling this is asserting that the state it just saw *survives* a reload,
 * so it must not silently navigate somewhere else to recover.
 */
export async function reloadBooted(page: Page): Promise<void> {
  await expect(async () => {
    await page.reload();
    await expect(page.getByRole("button", { name: SIGNED_IN })).toBeVisible({
      timeout: 5_000,
    });
  }).toPass({ timeout: BOOT_BUDGET_MS });
}
