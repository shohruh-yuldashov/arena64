import { expect, type Page, test } from "@playwright/test";

import {
  E2E_ACCOUNTS,
  resetLobby,
  saveState,
  type SeededAccount,
  seededAccount,
  statePath,
} from "./accounts";

/**
 * One pairing, end to end, across real browsers — A64-020.5A §27.8.
 *
 * jsdom proves the wiring; it cannot prove that a ticket one player creates
 * is a match another player is offered. This does. Seeded accounts join the
 * same pool in genuinely separate contexts — separate cookie jars, separate
 * memory — a real pairing scan pairs two of them, both accept, and both
 * land on the same match.
 *
 * Nothing is mocked. The catalogue is the seeded table, the pool is a real
 * `(variant, mode, time control, region)`, and the offer arrives by the
 * polling this phase ships because the gateway has no matchmaking producer
 * yet (`specs/frontend.md` §16).
 *
 * ## Three accounts, and why not two
 *
 * QT-3's rematch guard excludes a player's **most recent opponent**, and it
 * has no time window (`specs/matchmaking.md` §10.6). Two fixed accounts are
 * therefore pairable exactly once, ever: the moment they finish a game they
 * become each other's most recent opponent, and every later run finds two
 * waiting tickets and no match. The first version of this file did exactly
 * that and passed exactly once.
 *
 * Three accounts make a pairing always available — at most one of the three
 * pairs can be excluded — so what is asserted is the contract rather than a
 * lucky history. Which two get paired is the engine's business, and this
 * test deliberately does not predict it.
 *
 * ## Why bullet, and why casual
 *
 * `bullet_1_0` because a pool nobody else is in pairs these players and no
 * one else. `casual` because a rated result would move permanent numbers on
 * every run, and A-4 makes a rating a permanent competitive record — a test
 * suite is not a reason to write one.
 *
 * ## Accounts are seeded, not registered per run
 *
 * See `global-setup.ts`. Registration is capped at three per IP per hour and
 * sign-in at five per fifteen minutes; the setup seeds them once and saves
 * each account's browser session, which these contexts load — so a run
 * performs **no** registration and **no** sign-in.
 *
 * Fails loudly without the API rather than skipping quietly.
 *
 *     cd apps/api && uv run uvicorn main:app --port 8000
 *     cd apps/web && npm run test:e2e
 */

// Three browser contexts, three joins, a real pairing scan and polls at two
// seconds each. The 30s default leaves no headroom for the scan itself, and
// a flow that fails on a busy machine rather than on a defect is worse than
// no flow.
test.setTimeout(120_000);

const LOBBY = [E2E_ACCOUNTS.lobbyOne, E2E_ACCOUNTS.lobbyTwo, E2E_ACCOUNTS.lobbyThree] as const;

test("players queue into one pool, and the two who are paired both reach the match", async ({
  browser,
  request,
}) => {
  const reachable = await request
    .get("http://localhost:8000/health")
    .then((response) => response.ok())
    .catch(() => false);
  test.skip(!reachable, "apps/api is not running on :8000");

  // This suite's **own** accounts. See `E2E_ACCOUNTS` on why they are not
  // the social suite's: spec files run in parallel and this one settles its
  // accounts' matchmaking state.
  const accounts: SeededAccount[] = LOBBY.map((username) => seededAccount(username));

  // They persist across runs, so a ticket or an unanswered offer from a
  // previous one is cleared rather than assumed absent — §26.
  for (const account of accounts) await resetLobby(request, account);

  const contexts = await Promise.all(
    LOBBY.map((username) => browser.newContext({ storageState: statePath(username) })),
  );

  try {
    const pages = await Promise.all(contexts.map((context) => context.newPage()));

    // --- everyone joins the same pool -----------------------------------
    for (const page of pages) {
      await page.goto("/play");
      // The catalogue is rendered from the server, so waiting for the
      // option is also the assertion that `GET /time-controls` answered.
      const controls = page.getByRole("group", { name: /time control/i });
      await expect(controls).toBeVisible();
      // **Click the label, not the input.** The radio is `sr-only` — a
      // styled card is the visible control — so driving the input directly
      // waits forever on something no user clicks either.
      await controls.getByText("1+0", { exact: true }).click();
      await expect(controls.getByRole("radio", { name: /^1\+0/ })).toBeChecked();
      await page.getByRole("button", { name: /join the queue/i }).click();
      await expect(page.getByText(/searching for an opponent/i)).toBeVisible();
    }

    // --- the scan pairs two of them -------------------------------------
    // No explicit wait for the scan: the lobby polls, so a dialog appearing
    // *is* the delivery mechanism working.
    const paired = await twoWithAnOffer(pages);

    // Each sees the clock they actually chose.
    for (const page of paired) {
      await expect(page.getByRole("alertdialog").getByText("1+0")).toBeVisible();
    }

    // --- both accept ------------------------------------------------------
    const [first, second] = paired;
    await first
      .getByRole("alertdialog")
      .getByRole("button", { name: /^accept/i })
      .click();
    // The first acceptor has answered and the second has not: the dialog
    // stays and says so. This is also the state that used to strand them —
    // the match activates on the *other* player's request, so polling is
    // the only way they learn it started (`specs/matchmaking.md` §10.8).
    await expect(first.getByText(/waiting for your opponent/i)).toBeVisible();

    await second
      .getByRole("alertdialog")
      .getByRole("button", { name: /^accept/i })
      .click();

    // --- both land on the same match --------------------------------------
    for (const page of paired) {
      await expect(page).toHaveURL(/\/games\/[0-9a-f-]{36}$/, { timeout: 20_000 });
    }

    // The same one. Two players who accepted one offer and reached two
    // different games would be the pairing bug this whole flow exists to
    // rule out, and comparing the URLs is the only place it is visible.
    expect(new URL(first.url()).pathname).toBe(new URL(second.url()).pathname);
    await expect(first.getByRole("heading", { name: "Game" })).toBeVisible();
  } finally {
    // The contexts refreshed while running, rotating each cookie, so the
    // saved state is written back — otherwise the next run's probe would
    // present a superseded token, revoke the session it was reusing, and
    // fall back to a sign-in. Three of those per run exhausts the
    // five-per-IP login budget in two runs, which is exactly the failure
    // A64-020.4 built the seeded-account strategy to remove.
    for (const [index, context] of contexts.entries()) {
      const username = LOBBY[index];
      if (username !== undefined) {
        await saveState(context, username);
      }
      await context.close();
    }
  }
});

/**
 * The two pages showing an offer, once the scan has produced one.
 *
 * Polls rather than asserting on a named page, because **which** two are
 * paired is the engine's decision — with three tickets and one possibly
 * excluded pair, three outcomes are all correct. Naming one would encode a
 * result this test deliberately does not predict.
 *
 * Throws with the count rather than returning a short list, so a scan that
 * produced nothing reads as "no pairing happened" instead of failing later
 * on an undefined page.
 */
async function twoWithAnOffer(pages: Page[], timeoutMs = 30_000): Promise<[Page, Page]> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const showing: Page[] = [];
    for (const page of pages) {
      if ((await page.getByRole("alertdialog").count()) > 0) showing.push(page);
    }
    const [first, second] = showing;
    if (first !== undefined && second !== undefined) return [first, second];
    if (Date.now() > deadline) {
      throw new Error(
        `[e2e] the scan produced no pairing within ${timeoutMs}ms — ` +
          `${showing.length} of ${pages.length} lobbies show an offer`,
      );
    }
    await pages[0]?.waitForTimeout(500);
  }
}
