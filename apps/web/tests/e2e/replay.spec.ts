import { expect, test } from "@playwright/test";

import { E2E_ACCOUNTS, saveState, seededAccount, statePath } from "./accounts";

/**
 * A finished game, played back — A64-020.5E §26, §27.8.
 *
 * jsdom proves the navigation against a fixture; it cannot prove that the
 * archive a real backend reconstructs from a real move log renders. This
 * does: it finds a match this account actually finished, opens the replay
 * route, steps through it, jumps to the end, reloads, and confirms the same
 * position and result come back.
 *
 * ## Where the completed match comes from
 *
 * §26 forbids a backdoor and forbids depending on a match that may vanish
 * or stay active forever. The lobby chain already produces one:
 * `game-controls.spec.ts` ends its game by **resignation**, so by the time
 * this project runs, `e2e_lobby_*` have a completed match — and
 * `realtime-push.spec.ts` leaves another that flags on its one-minute
 * clock.
 *
 * This finds it through the **supported read** rather than a stored id:
 * `GET /players/{id}/matches` is the player's own history, which is what a
 * match-history UI would use. No table is truncated, no Redis is flushed,
 * no rate limit is disabled, and nothing is skipped silently — if the
 * account has no completed match, the chain that should have produced one
 * is broken and this says so.
 */
test.setTimeout(120_000);

test("a finished game replays, and survives a reload", async ({ browser, request }) => {
  const reachable = await request
    .get("http://localhost:8000/health")
    .then((response) => response.ok())
    .catch(() => false);
  test.skip(!reachable, "apps/api is not running on :8000");

  // Every lobby account is asked, because which of the three ended up in
  // the finished match depends on how the pairing fell.
  const candidates = [
    E2E_ACCOUNTS.lobbyOne,
    E2E_ACCOUNTS.lobbyTwo,
    E2E_ACCOUNTS.lobbyThree,
  ] as const;

  let found: { username: string; matchId: string } | null = null;
  for (const username of candidates) {
    const account = seededAccount(username);
    const response = await request.get(
      `http://localhost:8000/api/v1/players/${account.id}/matches?limit=20`,
      { headers: { Authorization: `Bearer ${account.accessToken}` }, failOnStatusCode: false },
    );
    if (!response.ok()) continue;

    const body = (await response.json()) as {
      data: { entries: { match_id: string; outcome: string | null }[] };
    };
    const finished = body.data.entries.find((entry) => entry.outcome !== null);
    if (finished !== undefined) {
      found = { username, matchId: finished.match_id };
      break;
    }
  }

  if (found === null) {
    throw new Error(
      "[e2e] no completed match on any lobby account — the lobby project chain " +
        "should have produced one. Run the full suite rather than this project alone.",
    );
  }

  const context = await browser.newContext({ storageState: statePath(found.username) });
  try {
    const page = await context.newPage();

    // Count the reads: a replay is one request for the whole game, and
    // stepping through it must add none (§23).
    // The **API** call only. Matching on `/replay` alone would also count
    // the document navigation to this very route and the lazy chunk named
    // after it, which is how this first read as three requests.
    let replayReads = 0;
    page.on("request", (req) => {
      if (req.url().includes(`/api/v1/matches/${found?.matchId}/replay`)) replayReads += 1;
    });

    await page.goto(`/games/${found.matchId}/replay`);

    const board = page.getByRole("grid", { name: /draughts board/i });
    await expect(board).toBeVisible({ timeout: 30_000 });
    // The opening position, reconstructed by the server from its own log.
    await expect(board.getByRole("gridcell", { name: /^c3, Light, man/ })).toBeVisible();
    await expect(page.getByRole("button", { name: /beginning/i })).toBeDisabled();

    // The result is there before any navigation — a replay is metadata as
    // well as a board.
    await expect(page.getByRole("alert")).toBeVisible();

    // Step forward, then jump to the end by keyboard.
    await page.getByRole("button", { name: /^next$/i }).click();
    await expect(page.getByRole("button", { name: /beginning/i })).toBeEnabled();

    await page.keyboard.press("End");
    await expect(page.getByRole("button", { name: /^end$/i })).toBeDisabled();
    const finalBoard = await board.innerText();

    // The whole game cost one request, however far it was stepped.
    expect(replayReads).toBe(1);

    // --- a reload returns the same archive -------------------------------
    // Nothing is in storage, so what comes back is what the server
    // reconstructed. The position after the reload is the *opening* again
    // — navigation is local presentation state, deliberately not persisted
    // (§12) — so this asserts the archive rather than the cursor.
    await page.reload();
    await expect(board).toBeVisible({ timeout: 30_000 });
    await page.keyboard.press("End");
    await expect(page.getByRole("button", { name: /^end$/i })).toBeDisabled();
    expect(await board.innerText()).toBe(finalBoard);

    // And the result survived it.
    await expect(page.getByRole("alert")).toBeVisible();
  } finally {
    await saveState(context, found.username);
    await context.close();
  }
});
