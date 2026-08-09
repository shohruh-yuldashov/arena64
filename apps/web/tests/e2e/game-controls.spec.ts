import { expect, type Page, test } from "@playwright/test";

import { E2E_ACCOUNTS, resetLobby, saveState, seededAccount, statePath } from "./accounts";

/**
 * The participant controls, across two real browsers — A64-020.5C §21.8.
 *
 * jsdom proves the reducer and the wiring; it cannot prove that a draw
 * offer one player makes reaches the other, that the server's spam rule
 * actually refuses an immediate re-offer, or that a resignation ends the
 * game on both screens with the same result. This does, against the real
 * gateway with nothing mocked.
 *
 * ## The whole negotiation in one test, deliberately
 *
 * §21 caps this phase at eight tests and seven are unit tests, so this is
 * the one E2E — and the flow it covers is a sequence rather than a set:
 * offer, decline, refused re-offer, a move, a permitted re-offer, then a
 * resignation. Splitting it would mean pairing four more matches, and each
 * pairing costs a QT-3 exclusion and a minute of clock.
 *
 * ## The spam rule is asserted from the client's side
 *
 * The interesting assertion is not that the server refuses — that is
 * `tests/contract/test_game_commands.py`'s — but that the **button is
 * disabled**, because the snapshot said so. That is the whole of §2: the
 * client renders the server's answer rather than computing one.
 *
 * Three accounts for the reason `play.spec.ts` and `game.spec.ts` use
 * three: QT-3 excludes a player's most recent opponent with no time window.
 */
test.setTimeout(180_000);

const LOBBY = [E2E_ACCOUNTS.lobbyOne, E2E_ACCOUNTS.lobbyTwo, E2E_ACCOUNTS.lobbyThree] as const;

test("two players negotiate a draw and one resigns", async ({ browser, request }) => {
  const reachable = await request
    .get("http://localhost:8000/health")
    .then((response) => response.ok())
    .catch(() => false);
  test.skip(!reachable, "apps/api is not running on :8000");

  for (const username of LOBBY) await resetLobby(request, seededAccount(username));

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
      // `bullet_1_0`, matching the other lobby specs: a game this suite
      // leaves active flags in a minute rather than blocking its accounts
      // for ten. The negotiation below spends seconds, not minutes.
      await controls.getByText("1+0", { exact: true }).click();
      await page
        .getByRole("button", { name: /find an opponent|raqib topish|найти соперника/i })
        .click();
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
      await expect(page.getByRole("grid", { name: /draughts board/i })).toBeVisible({
        timeout: 30_000,
      });
    }

    // **The offerer is the player who does *not* have the move**, and that
    // is the whole reason the roles are assigned rather than fixed.
    //
    // §3's rule is "one further move by the opponent". If the offerer held
    // the turn, satisfying it would take two moves — theirs and then the
    // opponent's — because the opponent's next move only comes after their
    // own. Giving the turn to the responder makes the assertion below about
    // exactly one move, which is what the rule says.
    const responder = (await hasTurn(first)) ? first : second;
    const offerer = responder === first ? second : first;

    // --- the offer reaches the other browser -----------------------------
    await offerer.getByRole("button", { name: /offer a draw/i }).click();

    // The offerer's durable state: a panel, not a toast that vanishes.
    await expect(offerer.getByText(/draw offer sent/i)).toBeVisible({ timeout: 30_000 });
    // And the offerer is not shown the answer buttons — §6.
    await expect(offerer.getByRole("button", { name: /accept draw/i })).toHaveCount(0);

    // The recipient, in a different browser, sees it.
    await expect(responder.getByRole("button", { name: /accept draw/i })).toBeVisible({
      timeout: 30_000,
    });

    // --- decline ---------------------------------------------------------
    await responder.getByRole("button", { name: /decline draw/i }).click();

    // Gone on both screens, and the game is still being played.
    await expect(responder.getByRole("button", { name: /accept draw/i })).toHaveCount(0, {
      timeout: 30_000,
    });
    await expect(offerer.getByText(/draw offer sent/i)).toHaveCount(0, { timeout: 30_000 });
    await expect(offerer.getByRole("grid", { name: /draughts board/i })).toBeVisible();

    // --- the spam rule, seen from the client -----------------------------
    // The offerer may not ask again until the opponent has moved. The
    // button is disabled because the **server** said so, and a reload
    // proves the answer came from the snapshot rather than from memory.
    await offerer.reload();
    await expect(offerer.getByRole("button", { name: /offer a draw/i })).toBeDisabled({
      timeout: 30_000,
    });

    // --- one opponent move, and the permitted state returns --------------
    // The responder holds the turn, so this is the single move §3 asks for.
    // c3–d4 for LIGHT, b6–a5 for DARK from the opening position.
    await expect(responder.getByText(/your turn/i)).toBeVisible({ timeout: 30_000 });
    const movesLight = await responder
      .getByRole("gridcell", { name: /^c3, Light, man/ })
      .isEnabled()
      .catch(() => false);
    const from = movesLight ? /^c3, Light, man/ : /^b6, Dark, man/;
    const destination = movesLight ? "d4" : "a5";

    await responder.getByRole("gridcell", { name: from }).click();
    await responder
      .getByRole("gridcell", { name: new RegExp(`^${destination}, empty`) })
      .click();

    await expect(
      offerer.getByRole("gridcell", { name: new RegExp(`^${destination}, (Light|Dark), man`) }),
    ).toBeVisible({ timeout: 30_000 });

    // Exactly one opponent move restores eligibility — from the server's
    // snapshot, not from a local timer.
    await expect(offerer.getByRole("button", { name: /offer a draw/i })).toBeEnabled({
      timeout: 30_000,
    });

    // --- resignation ends it identically on both screens -----------------
    await offerer.getByRole("button", { name: /^resign$/i }).click();
    const dialog = offerer.getByRole("alertdialog");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: /^resign$/i }).click();

    // The resigning player lost; the other won. Both read the result from
    // `game.completed`, not from what they clicked.
    await expect(offerer.getByText(/you lost/i)).toBeVisible({ timeout: 30_000 });
    await expect(responder.getByText(/you won/i)).toBeVisible({ timeout: 30_000 });
    for (const page of paired) {
      await expect(page.getByText(/resignation/i)).toBeVisible({ timeout: 30_000 });
      // Controls are gone from a finished game.
      await expect(page.getByRole("button", { name: /offer a draw/i })).toHaveCount(0);
    }
  } finally {
    for (const [index, context] of contexts.entries()) {
      const username = LOBBY[index];
      if (username !== undefined) await saveState(context, username);
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
