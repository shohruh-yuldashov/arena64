import { execFile } from "node:child_process";
import { resolve } from "node:path";
import { promisify } from "node:util";

import { expect, test } from "@playwright/test";

/**
 * Registration to a verified account, against the real backend —
 * A64-021.5H §31.12.
 *
 * ## What this proves that no other test can
 *
 * That the three halves agree: registration lands on the verification
 * screen, a **product route bounces back to it** while the address is
 * unconfirmed, and the same route opens once it is. Each half is asserted
 * elsewhere in isolation; only a browser against a real API shows that the
 * guard, the session and the backend policy tell one story.
 *
 * ## How the account becomes verified here
 *
 * Through `python -m app.operator.accounts verify` — the repository's own
 * operator entry point, the same class of command `tournament.spec.ts` uses
 * to create a fixture. **Not** by typing a code, and the reason is a rule
 * rather than convenience: §31 forbids an automated test reading a real
 * inbox and forbids exposing the code through a browser-reachable endpoint
 * for testing. There is no captured-email boundary in this suite.
 *
 * What that leaves uncovered *here* is the six digits themselves, and they
 * are covered where they can be: `tests/contract/test_otp_verification.py`
 * reads the code out of the delivered message and submits it through the
 * real API, and `verify-email.test.tsx` pastes one into the real form.
 * This spec covers the part only a browser can — the navigation.
 */
const run = promisify(execFile);

/** The API directory, from `apps/web`. */
const API_DIR = resolve(process.cwd(), "..", "api");

const UNIQUE = process.env.ARENA64_E2E_SUFFIX ?? String(Date.now());

test("registration lands on verification, product routes wait, and verifying opens them", async ({
  page,
  request,
}) => {
  const reachable = await request
    .get("http://localhost:8000/health")
    .then((response) => response.ok())
    .catch(() => false);
  test.skip(!reachable, "apps/api is not running on :8000");

  const email = `otp${UNIQUE}@example.com`;

  await page.goto("/register");
  await page.getByLabel(/username/i).fill(`otp${UNIQUE}`.slice(0, 20));
  await page.getByLabel(/^email/i).fill(email);
  await page.getByLabel(/^password/i).fill("CorrectHorse1!");
  await page.getByLabel(/confirm password/i).fill("CorrectHorse1!");
  await page.getByRole("button", { name: /create account/i }).click();

  // --- signed in, unverified, and on the one page with something to do ---
  await expect(page.getByRole("button", { name: /^(Account|Hisob|Аккаунт)$/ })).toBeVisible();
  await expect(page).toHaveURL(/\/verify-email/);
  const field = page.getByLabel(/verification code|tasdiqlash kodi/i);
  await expect(field).toBeVisible();
  // The attribute that makes this usable on a phone, asserted in a real
  // browser because it is invisible on a desktop when it is missing.
  await expect(field).toHaveAttribute("autocomplete", "one-time-code");

  // --- a product route does not open, and does not error ---
  await page.goto("/friends");
  await expect(page).toHaveURL(/\/verify-email/);
  await expect(page.getByLabel(/verification code|tasdiqlash kodi/i)).toBeVisible();

  // --- verified out of band, the way support would ---
  const { stdout } = await run(
    "uv",
    ["run", "python", "-m", "app.operator.accounts", "verify", "--email", email],
    { cwd: API_DIR },
  );
  expect(stdout).toContain("verified");

  // --- and the same route opens ---
  //
  // Reloaded **first**, and that ordering is the point: the session in this
  // tab still holds the `is_verified: false` it was given at registration,
  // so navigating without re-bootstrapping would bounce off the guard using
  // a stale answer. A reload re-reads the account from the server, which is
  // §22's claim that the session response — not anything this client
  // remembers — is the authority for this state.
  await page.reload();
  await expect(page.getByRole("button", { name: /^(Account|Hisob|Аккаунт)$/ })).toBeVisible();

  await page
    .getByRole("link", { name: /friends/i })
    .first()
    .click();
  await expect(page).toHaveURL(/\/friends$/);
  await expect(page.getByLabel(/verification code|tasdiqlash kodi/i)).toBeHidden();
});
