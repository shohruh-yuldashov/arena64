import { expect, test } from "@playwright/test";

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
const UNIQUE = process.env.ARENA64_E2E_SUFFIX ?? String(Date.now());

test("a player edits their profile and sees it on their public page", async ({
  page,
  request,
}) => {
  const reachable = await request
    .get("http://localhost:8000/health")
    .then((response) => response.ok())
    .catch(() => false);
  test.skip(!reachable, "apps/api is not running on :8000");

  const username = `p${UNIQUE}`.slice(0, 20);

  await page.goto("/register");
  await page.getByLabel(/username/i).fill(username);
  await page.getByLabel(/^email/i).fill(`${username}@example.com`);
  await page.getByLabel(/^password/i).fill("CorrectHorse1!");
  await page.getByLabel(/confirm password/i).fill("CorrectHorse1!");
  await page.getByRole("button", { name: /create account/i }).click();
  await expect(page.getByRole("button", { name: /sign out/i })).toBeVisible();

  // --- the profile is reachable from the header, not only by URL ---------
  await page.getByRole("link", { name: new RegExp(username, "i") }).click();
  await expect(page).toHaveURL(/\/profile$/);
  await expect(page.getByRole("heading", { level: 1, name: username })).toBeVisible();

  // --- edit ---------------------------------------------------------------
  await page.getByRole("link", { name: /edit profile/i }).click();
  await expect(page).toHaveURL(/\/settings\/profile$/);

  const save = page.getByRole("button", { name: /^save$/i });
  // Nothing has changed yet, so there is nothing to send.
  await expect(save).toBeDisabled();

  await page.getByLabel(/display name/i).fill("Edited Name");
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
  await expect(page.getByLabel(/display name/i)).toHaveValue("Edited Name");

  // --- and it is on the public page, which is a different read ----------
  await page.goto(`/players/${username}`);
  await expect(page.getByRole("heading", { level: 1, name: "Edited Name" })).toBeVisible();
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
});
