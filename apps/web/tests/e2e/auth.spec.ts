import { expect, test } from "@playwright/test";

/**
 * One complete authentication journey, across the **real** boundary —
 * A64-020.2 §20.8.
 *
 * Everything else in this phase stops at a boundary: the backend tests use
 * a real database and a test client, the frontend tests use a real browser
 * runtime and a mocked network. Neither can see the thing this test is for
 * — that a real browser, given a real `Set-Cookie` from a real FastAPI
 * process, keeps it, sends it back on the next request, and is still signed
 * in after a reload.
 *
 * That is the whole session design in one sentence, and no mock can prove
 * it: MSW does not implement cookie jars, `HttpOnly`, `Path` scoping or
 * `SameSite`, and a test client does not implement any of them the way a
 * browser does.
 *
 * ## Why it is skipped rather than failed without the API
 *
 * It needs `apps/api` on port 8000 with its database — the same posture the
 * backend's own contract suite takes when PostgreSQL is unreachable
 * (`tests/contract/conftest.py`). A suite that fails when infrastructure is
 * absent is a suite people learn to ignore.
 *
 * Run it with the API up:
 *
 *     cd apps/api && uv run uvicorn app.app_factory:create_app --factory --port 8000
 *     cd apps/web && npm run test:e2e
 */
const UNIQUE = process.env.ARENA64_E2E_SUFFIX ?? String(Date.now());

test("a player registers, reloads, stays signed in, and signs out", async ({
  page,
  request,
}) => {
  const reachable = await request
    .get("http://localhost:8000/health")
    .then((response) => response.ok())
    .catch(() => false);
  test.skip(!reachable, "apps/api is not running on :8000");

  await page.goto("/register");

  await page.getByLabel(/username/i).fill(`e2e${UNIQUE}`.slice(0, 20));
  await page.getByLabel(/^email/i).fill(`e2e${UNIQUE}@example.com`);
  await page.getByLabel(/^password/i).fill("CorrectHorse1!");
  await page.getByLabel(/confirm password/i).fill("CorrectHorse1!");
  await page.getByRole("button", { name: /create account/i }).click();

  // Registration signs the browser in and lands on the app.
  await expect(page.getByRole("button", { name: /sign out/i })).toBeVisible();
  expect(new URL(page.url()).pathname).toBe("/");

  // --- the cookie is real, and unreachable from script ---
  const cookies = await page.context().cookies();
  const refresh = cookies.find((cookie) => cookie.name === "arena64_refresh");
  expect(refresh, "the refresh cookie was not set").toBeDefined();
  expect(refresh?.httpOnly).toBe(true);
  expect(refresh?.path).toBe("/api/v1/auth/browser");
  // The page cannot see it. This is the assertion the whole design exists
  // for, and the only place it can be made against a real browser.
  expect(await page.evaluate(() => document.cookie)).not.toContain("arena64_refresh");

  // --- and nothing was written where a script could read it ---
  const stored = await page.evaluate(() =>
    JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }),
  );
  expect(stored).not.toContain("access_token");
  expect(stored).not.toContain("Bearer");

  // --- the reload: the access token is gone, the cookie is not ---
  await page.reload();
  await expect(page.getByRole("button", { name: /sign out/i })).toBeVisible();

  // --- sign out clears both halves ---
  await page.getByRole("button", { name: /sign out/i }).click();
  await expect(page.getByRole("link", { name: /sign in/i })).toBeVisible();
  const afterLogout = await page.context().cookies();
  expect(afterLogout.find((cookie) => cookie.name === "arena64_refresh")?.value ?? "").toBe("");

  // And it stays signed out across a reload — a cookie that survived would
  // mean `delete_cookie` and `set_cookie` disagreed about `Path`.
  await page.reload();
  await expect(page.getByRole("link", { name: /sign in/i })).toBeVisible();
});
