import { expect, test } from "@playwright/test";

import { openAccountMenu } from "./session";

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

  // **A64-021.5H.** Registration signs the browser in and lands on the
  // verification screen, not the app: the session exists and the address
  // does not, so every product write behind the app would answer `403`.
  await expect(page.getByRole("button", { name: /^(Account|Hisob|Аккаунт)$/ })).toBeVisible();
  expect(new URL(page.url()).pathname).toBe("/verify-email");
  await expect(page.getByLabel(/verification code|tasdiqlash kodi/i)).toBeVisible();

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
  //
  // Still on the verification screen afterwards, which is §22's claim: no
  // part of that state lives in this component, so a reload rebuilds it
  // from the session and the server rather than losing it.
  await page.reload();
  await expect(page.getByRole("button", { name: /^(Account|Hisob|Аккаунт)$/ })).toBeVisible();
  await expect(page.getByLabel(/verification code|tasdiqlash kodi/i)).toBeVisible();

  // --- sign out clears both halves ---
  //
  // The signed-out signal is the **header's** sign-in link, matched by its
  // exact name — and both halves of that are load-bearing since A64-021.5H
  // moved this moment onto `/verify-email`:
  //
  //   `/verify-email` renders a "Go to sign in" link of its own, which a
  //     `/sign in/i` pattern matches while still signed *in*;
  //   the sign-out button's accessible name becomes the spinner's the
  //     instant it is clicked, so waiting for it to disappear is waiting
  //     for the click rather than for the request.
  //
  // Either one reads the cookie mid-flight and fails intermittently. The
  // banner's link appears only for `anonymous`, which the session reaches
  // after the logout response has been applied.
  // A64-025.13 §35.5. Sign-out lives inside the account menu since
  // A64-025.9B, so reaching it is two steps now.
  await openAccountMenu(page);
  await page.getByRole("button", { name: /sign out/i }).click();
  await expect(page.getByRole("banner").getByRole("link", { name: "Sign in" })).toBeVisible();
  const afterLogout = await page.context().cookies();
  expect(afterLogout.find((cookie) => cookie.name === "arena64_refresh")?.value ?? "").toBe("");

  // And it stays signed out across a reload — a cookie that survived would
  // mean `delete_cookie` and `set_cookie` disagreed about `Path`.
  await page.reload();
  await expect(page.getByRole("banner").getByRole("link", { name: "Sign in" })).toBeVisible();
});
