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
 * One live game, across two real browsers — A64-020.5B §32.10.
 *
 * jsdom proves the reducer; it cannot prove that a move one player makes
 * over a WebSocket is a move the other player sees. This does: two seeded
 * accounts are paired through the real queue, both open the board, one
 * moves, the other's board changes, and a reload recovers the position from
 * the server rather than from anything the browser kept.
 *
 * Nothing is mocked. Real gateway, real ticket, real engine, real clock.
 *
 * ## The match is made through the product, not a fixture
 *
 * §31 prefers the supported flow and forbids a hidden gameplay backdoor, so
 * this queues both players and accepts the pairing exactly as a person
 * would. That also means this suite exercises A64-020.5A's lobby on every
 * run, which is a bonus rather than a cost.
 *
 * Three accounts for the reason `play.spec.ts` uses three: QT-3 excludes a
 * player's most recent opponent with no time window, so a fixed pair is
 * pairable once ever.
 */
test.setTimeout(180_000);

const LOBBY = [E2E_ACCOUNTS.lobbyOne, E2E_ACCOUNTS.lobbyTwo, E2E_ACCOUNTS.lobbyThree] as const;

test("two players share one live board over the socket", async ({ browser, request }) => {
  const reachable = await request
    .get("http://localhost:8000/health")
    .then((response) => response.ok())
    .catch(() => false);
  test.skip(!reachable, "apps/api is not running on :8000");

  const accounts: SeededAccount[] = LOBBY.map((username) => seededAccount(username));
  for (const account of accounts) await resetLobby(request, account);

  const contexts = await Promise.all(
    LOBBY.map((username) => browser.newContext({ storageState: statePath(username) })),
  );

  try {
    const pages = await Promise.all(contexts.map((context) => context.newPage()));

    // --- pair two of them through the real lobby ------------------------
    for (const page of pages) {
      await page.goto("/play");
      const controls = page.getByRole("group", { name: /time control/i });
      await expect(controls).toBeVisible();
      // `bullet_1_0`, matching `play.spec.ts`, and the reason is cleanup
      // rather than speed. A game this suite leaves active blocks its
      // accounts from queueing again — `pending_for` reports a live game
      // since A64-020.5B, and the lobby correctly sends that player to it —
      // so `resetLobby` waits for the clock to flag. Sixty seconds is a
      // wait; ten minutes is a suite nobody runs.
      //
      // The game itself is in no danger from it: a clock runs only for the
      // side to move, and this flow spends a few seconds on one move.
      await controls.getByText("1+0", { exact: true }).click();
      await page.getByRole("button", { name: /join the queue/i }).click();
      await expect(page.getByText(/searching for an opponent/i)).toBeVisible();
    }

    const paired = await twoWithAnOffer(pages);
    for (const page of paired) {
      await page
        .getByRole("alertdialog")
        .getByRole("button", { name: /^accept/i })
        .click();
    }

    const [first, second] = paired;
    for (const page of paired) {
      await expect(page).toHaveURL(/\/games\/[0-9a-f-]{36}$/, { timeout: 30_000 });
    }
    expect(new URL(first.url()).pathname).toBe(new URL(second.url()).pathname);

    // --- both boards load from the authoritative snapshot ---------------
    for (const page of paired) {
      await expect(page.getByRole("grid", { name: /draughts board/i })).toBeVisible({
        timeout: 30_000,
      });
      // A man on c3 is the opening position — proof the snapshot was
      // applied rather than an empty board being rendered.
      await expect(page.getByRole("gridcell", { name: /^c3, Light, man/ })).toBeVisible({
        timeout: 30_000,
      });
    }

    // --- whoever has the move plays it ----------------------------------
    const mover = (await hasTurn(first)) ? first : second;
    const waiter = mover === first ? second : first;
    await expect(mover.getByText(/your turn/i)).toBeVisible({ timeout: 30_000 });

    // c3–d4 is legal from the opening position for LIGHT; b6–a5 for DARK.
    const movesLight = await mover
      .getByRole("gridcell", { name: /^c3, Light, man/ })
      .isEnabled()
      .catch(() => false);

    const from = movesLight ? /^c3, Light, man/ : /^b6, Dark, man/;
    const destination = movesLight ? "d4" : "a5";

    await mover.getByRole("gridcell", { name: from }).click();
    // The kernel lit the destination up before any server round trip — the
    // whole reason it exists.
    await expect(
      mover.getByRole("gridcell", { name: new RegExp(`^${destination}, empty.*legal move`) }),
    ).toBeVisible();
    await mover.getByRole("gridcell", { name: new RegExp(`^${destination}, empty`) }).click();

    // --- the opponent sees it -------------------------------------------
    const landed = new RegExp(`^${destination}, (Light|Dark), man`);
    for (const page of paired) {
      await expect(page.getByRole("gridcell", { name: landed })).toBeVisible({
        timeout: 30_000,
      });
    }
    // And the turn passed, on the waiting player's own screen.
    await expect(waiter.getByText(/your turn/i)).toBeVisible({ timeout: 30_000 });

    // --- a reload recovers from the server ------------------------------
    // Nothing is in storage, so what comes back is what `game.resume`
    // returned — which is the whole of §18 asserted in one line.
    await waiter.reload();
    await expect(waiter.getByRole("gridcell", { name: landed })).toBeVisible({
      timeout: 30_000,
    });
    await expect(waiter.getByText(/your turn/i)).toBeVisible({ timeout: 30_000 });
  } finally {
    for (const [index, context] of contexts.entries()) {
      const username = LOBBY[index];
      if (username !== undefined) {
        await saveState(context, username);
      }
      await context.close();
    }
  }
});

async function hasTurn(page: Page): Promise<boolean> {
  return page
    .getByText(/your turn/i)
    .isVisible()
    .catch(() => false);
}

async function twoWithAnOffer(pages: Page[], timeoutMs = 40_000): Promise<[Page, Page]> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const showing: Page[] = [];
    for (const page of pages) {
      if ((await page.getByRole("alertdialog").count()) > 0) showing.push(page);
    }
    const [first, second] = showing;
    if (first !== undefined && second !== undefined) return [first, second];
    if (Date.now() > deadline) {
      throw new Error(`[e2e] no pairing within ${timeoutMs}ms — ${showing.length} offers`);
    }
    await pages[0]?.waitForTimeout(500);
  }
}
