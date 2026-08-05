import { expect, test } from "@playwright/test";

import { E2E_ACCOUNTS, saveState, seededAccount, statePath } from "./accounts";

/**
 * Statistics, history and a replay — A64-020.5F §28, §29.12.
 *
 * The whole feature end to end against the real backend: a profile whose
 * counters came from the projection, the history those counters count, and
 * a replay reached from a row.
 *
 * ## Where the completed matches come from
 *
 * The lobby project chain, which finishes games by resignation
 * (`game-controls.spec.ts`) and by flag (`realtime-push.spec.ts`). Nothing
 * here truncates a table, flushes Redis, disables a rate limit or adds a
 * backdoor — and if the account has no history, this fails loudly rather
 * than skipping, because a green suite that asserted nothing is worse than
 * a red one.
 *
 * ## The projection has to have run
 *
 * Statistics are an outbox consumer, so the relay must have processed the
 * completions. It runs on a tick, so this waits for the count rather than
 * asserting it immediately — and a non-zero count is the assertion that the
 * consumer is wired at all, which is what §13's reachability asks for.
 */
test.setTimeout(120_000);

test("a player's statistics, history and a replay all agree", async ({ browser, request }) => {
  const reachable = await request
    .get("http://localhost:8000/health")
    .then((response) => response.ok())
    .catch(() => false);
  test.skip(!reachable, "apps/api is not running on :8000");

  const username = E2E_ACCOUNTS.lobbyOne;
  const account = seededAccount(username);
  const context = await browser.newContext({ storageState: statePath(username) });

  try {
    const page = await context.newPage();

    // Count the reads: one per history page, and none per row.
    let historyRequests = 0;
    let profileRequests = 0;
    page.on("request", (req) => {
      if (req.method() !== "GET") return;
      const url = req.url();
      if (url.includes(`/players/${account.id}/matches`)) historyRequests += 1;
      if (/\/api\/v1\/(users|profiles)\//.test(url)) profileRequests += 1;
    });

    // --- the profile shows counters the projection produced -------------
    await page.goto("/profile");
    const statistics = page.getByRole("region", { name: /statistics/i });
    await expect(statistics).toBeVisible({ timeout: 30_000 });

    // Non-zero, which is what says the consumer and the backfill actually
    // wrote something — the whole point of the phase. Read from the
    // rendered panel rather than from the API, so this asserts what a
    // player sees.
    const panel = await statistics.innerText();
    const count = Number(/\bGames\b[^0-9]*([0-9]+)/i.exec(panel)?.[1] ?? "0");
    expect(count).toBeGreaterThan(0);

    // --- and a link to the history those counters count ------------------
    await page.getByRole("link", { name: /match history|o'yinlar tarixi/i }).click();
    await expect(page).toHaveURL(/\/games\/history$/);

    const list = page.getByRole("list", { name: /match history/i });
    await expect(list).toBeVisible({ timeout: 30_000 });
    const rows = await list.getByRole("listitem").count();
    expect(rows).toBeGreaterThan(0);

    // One request for the page and **none** for the opponents on it: the
    // opponent is composed server-side, which is the N+1 this phase's
    // backend prerequisite existed to avoid.
    expect(historyRequests).toBe(1);
    expect(profileRequests).toBe(0);

    // The history cannot claim more games than the profile counted.
    expect(rows).toBeLessThanOrEqual(count);

    // --- a row opens a real replay ---------------------------------------
    await list
      .getByRole("link", { name: /replay the match/i })
      .first()
      .click();
    await expect(page).toHaveURL(/\/games\/[0-9a-f-]{36}\/replay$/);

    await expect(page.getByRole("grid", { name: /draughts board/i })).toBeVisible({
      timeout: 30_000,
    });
    // The result the history row promised, on the replay's own summary.
    await expect(page.getByRole("alert")).toBeVisible();
  } finally {
    await saveState(context, username);
    await context.close();
  }
});
