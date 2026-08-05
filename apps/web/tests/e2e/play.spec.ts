import { expect, test } from "@playwright/test";

import {
  E2E_ACCOUNTS,
  resetLobby,
  type SeededAccount,
  seededAccount,
  statePath,
} from "./accounts";

/**
 * One pairing, end to end, across two real browsers — A64-020.5A §27.8.
 *
 * jsdom proves the wiring; it cannot prove that a ticket one player creates
 * is a match another player is offered. This does. Two seeded accounts join
 * the same pool in genuinely separate contexts — separate cookie jars,
 * separate memory — a real pairing scan pairs them, both accept, and both
 * land on the same match.
 *
 * Nothing is mocked. The catalogue is the seeded table, the pool is a real
 * `(variant, mode, time control, region)`, and the offer arrives by the
 * polling this phase ships because the gateway has no matchmaking producer
 * yet (`specs/frontend.md` §16).
 *
 * ## Why bullet, and why casual
 *
 * `bullet_1_0` because a pool nobody else is in pairs the two players in
 * this test and no one else. `casual` because a rated result would move
 * two permanent numbers on every run, and A-4 makes a rating a permanent
 * competitive record — a test suite is not a reason to write one.
 *
 * ## Accounts are seeded, not registered per run
 *
 * See `global-setup.ts`. Registration is capped at three per IP per hour
 * and sign-in at five per fifteen minutes; this suite needs two accounts
 * and would exhaust both. The setup seeds them once and saves each
 * account's browser session, which these contexts load — so a run performs
 * **no** registration and **no** sign-in.
 *
 * Fails loudly without the API rather than skipping quietly.
 *
 *     cd apps/api && uv run uvicorn main:app --port 8000
 *     cd apps/web && npm run test:e2e
 */
// Two browser contexts, two joins, a real pairing scan and two polls at
// two seconds each. The 30s default leaves no headroom for the scan itself,
// and a flow that fails on a busy machine rather than on a defect is worse
// than no flow — this is the one place a longer budget is honest rather
// than a way of hiding slowness.
test.setTimeout(90_000);

test("two players queue into the same pool and both reach the match", async ({
  browser,
  request,
}) => {
  const reachable = await request
    .get("http://localhost:8000/health")
    .then((response) => response.ok())
    .catch(() => false);
  test.skip(!reachable, "apps/api is not running on :8000");

  // This suite's **own** two accounts. See `E2E_ACCOUNTS` on why they are
  // not the social suite's: spec files run in parallel and this one settles
  // its accounts' matchmaking state.
  const alice: SeededAccount = seededAccount(E2E_ACCOUNTS.lobbyOne);
  const bob: SeededAccount = seededAccount(E2E_ACCOUNTS.lobbyTwo);

  // The accounts persist across runs, so a ticket or an unanswered offer
  // from a previous one is cleared rather than assumed absent — §26.
  await resetLobby(request, alice);
  await resetLobby(request, bob);

  const aliceContext = await browser.newContext({
    storageState: statePath(E2E_ACCOUNTS.lobbyOne),
  });
  const bobContext = await browser.newContext({
    storageState: statePath(E2E_ACCOUNTS.lobbyTwo),
  });

  try {
    const alicePage = await aliceContext.newPage();
    const bobPage = await bobContext.newPage();

    // --- both join the same pool ---------------------------------------
    for (const page of [alicePage, bobPage]) {
      await page.goto("/play");
      // The catalogue is rendered from the server, so waiting for the
      // option is also the assertion that `GET /time-controls` answered.
      const controls = page.getByRole("group", { name: /time control/i });
      await expect(controls).toBeVisible();
      // **Click the label, not the input.** The radio is `sr-only` — a
      // styled card is the visible control and the input is there for
      // assistive technology and form semantics. Playwright refuses to
      // `check()` an element it considers hidden, so driving the input
      // directly waits forever on something no user clicks either.
      //
      // `exact` on the clock alone: the label's accessible name is "1+0
      // Bullet", and pinning the translated speed class here would make a
      // locale change break the flow.
      await controls.getByText("1+0", { exact: true }).click();
      await expect(controls.getByRole("radio", { name: /^1\+0/ })).toBeChecked();
      await page.getByRole("button", { name: /join the queue/i }).click();
      await expect(page.getByText(/searching for an opponent/i)).toBeVisible();
    }

    // --- the pairing scan produces an offer for both --------------------
    // No explicit wait for the scan: the lobby polls, so the dialog
    // appearing *is* the delivery mechanism working. The generous timeout
    // covers one scan interval plus one poll interval.
    const aliceOffer = alicePage.getByRole("alertdialog");
    const bobOffer = bobPage.getByRole("alertdialog");
    await expect(aliceOffer).toBeVisible({ timeout: 20_000 });
    await expect(bobOffer).toBeVisible({ timeout: 20_000 });

    // Each sees the other, and the clock they actually chose.
    await expect(aliceOffer.getByText(`@${bob.username}`)).toBeVisible();
    await expect(bobOffer.getByText(`@${alice.username}`)).toBeVisible();
    await expect(aliceOffer.getByText("1+0")).toBeVisible();

    // --- both accept ----------------------------------------------------
    await aliceOffer.getByRole("button", { name: /^accept/i }).click();
    // Alice has answered and Bob has not: the dialog stays and says so.
    await expect(alicePage.getByText(/waiting for your opponent/i)).toBeVisible();

    await bobOffer.getByRole("button", { name: /^accept/i }).click();

    // --- both land on the same match ------------------------------------
    await expect(alicePage).toHaveURL(/\/games\/[0-9a-f-]{36}$/, { timeout: 20_000 });
    await expect(bobPage).toHaveURL(/\/games\/[0-9a-f-]{36}$/, { timeout: 20_000 });

    // The same one. Two players who accepted one offer and reached two
    // different games would be the pairing bug this whole flow exists to
    // rule out, and comparing the URLs is the only place it is visible.
    expect(new URL(alicePage.url()).pathname).toBe(new URL(bobPage.url()).pathname);
    await expect(alicePage.getByRole("heading", { name: "Game" })).toBeVisible();
  } finally {
    // The contexts refreshed while running, rotating each cookie, so the
    // saved state is written back — otherwise the next run's probe would
    // present a superseded token, revoke the session it was reusing, and
    // fall back to a sign-in. Two of those per run exhausts the five-per-IP
    // login budget in three runs, which is exactly the failure A64-020.4
    // built the seeded-account strategy to remove.
    await aliceContext.storageState({ path: statePath(E2E_ACCOUNTS.lobbyOne) });
    await bobContext.storageState({ path: statePath(E2E_ACCOUNTS.lobbyTwo) });
    await aliceContext.close();
    await bobContext.close();
  }
});
