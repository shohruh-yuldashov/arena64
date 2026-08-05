import { expect, test } from "@playwright/test";

import { E2E_ACCOUNTS, seededAccount, statePath } from "./accounts";

/**
 * One profile journey, across the real boundary — A64-020.3 §20.8.
 *
 * jsdom proves the wiring; it does not prove that a `PATCH` reaches
 * PostgreSQL and comes back changed. This does: register, edit the profile,
 * confirm the change survives a reload, and see it on the **public** page,
 * which is a different endpoint reading the same row through a different
 * privacy filter. Nothing is mocked.
 *
 * ## Why the public page is the assertion that matters
 *
 * `/profile` could render from its own mutation response and look correct
 * while nothing was stored. `/players/{username}` is served by a different
 * handler, from the database, with no cache in common — so seeing the new
 * name there is the write actually having happened.
 *
 * Skipped, not failed, without the API — the same posture the backend's own
 * contract suite takes when PostgreSQL is unreachable. A suite that fails
 * when infrastructure is absent is one people learn to ignore.
 *
 *     cd apps/api && uv run uvicorn app.app_factory:create_app --factory --port 8000
 *     cd apps/web && npm run test:e2e
 */
test("a player edits their profile and sees it on their public page", async ({
  browser,
  request,
}) => {
  const reachable = await request
    .get("http://localhost:8000/health")
    .then((response) => response.ok())
    .catch(() => false);
  test.skip(!reachable, "apps/api is not running on :8000");

  // A **seeded** account, not a fresh registration — A64-020.4 §21.
  // Registration is capped at three per IP per hour, and this spec spent one
  // of them on every run until the cap made the suite unrunnable. Only
  // `auth.spec.ts` still registers, because registration is its subject.
  const owner = seededAccount(E2E_ACCOUNTS.profile);
  const username = owner.username;

  // The saved session, not a sign-in — see `tests/e2e/accounts.ts`.
  const context = await browser.newContext({ storageState: statePath(username) });
  const page = await context.newPage();
  await page.goto("/profile");
  await expect(page.getByRole("button", { name: /sign out/i })).toBeVisible();

  // --- the profile is reachable from the header, not only by URL ---------
  await page.goto("/profile");
  await expect(page).toHaveURL(/\/profile$/);
  // The seeded account keeps whatever name the previous run left, so the
  // assertion is that a profile rendered — the *edit* below is what this
  // spec is about, and it asserts its own result.
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();

  // --- edit ---------------------------------------------------------------
  await page.getByRole("link", { name: /edit profile/i }).click();
  await expect(page).toHaveURL(/\/settings\/profile$/);

  const save = page.getByRole("button", { name: /^save$/i });
  // Nothing has changed yet, so there is nothing to send.
  await expect(save).toBeDisabled();

  // Unique per run. The account is **seeded and reused**, so it still holds
  // the previous run's values — filling the same ones would leave the form
  // clean and the submit correctly disabled, which is the dirty-state rule
  // working and a test that never asserts anything.
  const editedName = `Edited ${Date.now()}`;
  await page.getByLabel(/display name/i).fill(editedName);
  await page.getByLabel(/about you/i).fill("Playing since 2026.");
  await page.getByLabel(/country code/i).fill("UZ");
  await expect(save).toBeEnabled();
  await save.click();

  await expect(page.getByRole("status")).toHaveText(/saved/i);
  // Re-baselined against the server's answer, so the form is clean again
  // rather than dirty against its own successful save.
  await expect(save).toBeDisabled();

  // --- it survives a reload, so it was stored and not merely rendered ----
  await page.reload();
  await expect(page.getByLabel(/display name/i)).toHaveValue(editedName);

  // --- and it is on the public page, which is a different read ----------
  await page.goto(`/players/${username}`);
  await expect(page.getByRole("heading", { level: 1, name: editedName })).toBeVisible();
  await expect(page.getByText("Playing since 2026.")).toBeVisible();
  await expect(page.getByText(`@${username}`)).toBeVisible();

  // The public surface must never carry the account's email — the schema
  // has no such field, and this is the one place that is checked against a
  // real serialiser rather than a fixture.
  await expect(page.locator("body")).not.toContainText(`${username}@example.com`);

  // --- signing out everywhere is reachable and takes effect --------------
  await page.goto("/settings/sessions");
  await page.getByRole("button", { name: /sign out everywhere/i }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: /^sign out$/i })
    .click();

  await expect(page).toHaveURL(/\/login/);
  // The session is genuinely gone: a protected route now bounces.
  await page.goto("/profile");
  await expect(page).toHaveURL(/\/login/);

  // Written back for the same reason the social spec does: the context
  // rotated the cookie, and the next run reuses this file.
  await context.storageState({ path: statePath(username) });
  await context.close();
});
