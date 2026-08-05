import { expect, type Page, test } from "@playwright/test";

import { E2E_ACCOUNTS, resetLobby, saveState, seededAccount, statePath } from "./accounts";

/**
 * Match offers arrive by socket, and the lobby stops polling —
 * A64-020.5D §23.10.
 *
 * The claim this phase exists for, and the only place it can be made:
 * jsdom proves the reconciliation and the backend tests prove the routing,
 * but neither can show that a pairing made by the real pairing scan
 * reaches a real browser over a real socket — through the real relay, the
 * real outbox, the real `GatewayPendingMatchSink` and the real fan-out.
 *
 * ## The request count is the assertion, not a bonus
 *
 * §22 asks for a measurement and §23.10 asks that healthy realtime not poll
 * every two seconds. Counting `GET /matchmaking/*` while queued is how both
 * are checked rather than described — and it is the assertion that would
 * fail if `useWaitingInterval` were wired to the wrong status, which is a
 * mistake no unit test of a pure function would catch.
 *
 * Three accounts for the reason every lobby spec uses three: QT-3 excludes
 * a player's most recent opponent with no time window.
 */
test.setTimeout(180_000);

const LOBBY = [E2E_ACCOUNTS.lobbyOne, E2E_ACCOUNTS.lobbyTwo, E2E_ACCOUNTS.lobbyThree] as const;

/** How long the queued state is watched while counting requests. */
const OBSERVATION_MS = 20_000;

/**
 * The most `GET /matchmaking/*` requests one page may make while queued and
 * connected, over the observation window.
 *
 * Two queries on a 25-second safety interval make at most one request each
 * in twenty seconds; the initial reads and a mount-time refetch account for
 * the rest. **Ten** is loose enough not to be flaky and an order of
 * magnitude below the old behaviour, which was two queries at two seconds
 * — twenty requests in the same window, and this asserts fewer than half a
 * second's worth of that.
 */
const MAX_REQUESTS_WHILE_REALTIME = 10;

test("a pairing arrives over the socket and the lobby stops polling", async ({
  browser,
  request,
}) => {
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

    // Count every matchmaking read each page makes, from the moment it
    // loads. Counted per page because the assertion is per client.
    const counts = new Map<Page, number>();
    const sockets = new Map<Page, string[]>();
    for (const page of pages) {
      counts.set(page, 0);
      sockets.set(page, []);
      page.on("request", (req) => {
        if (req.method() === "GET" && req.url().includes("/api/v1/matchmaking/")) {
          counts.set(page, (counts.get(page) ?? 0) + 1);
        }
      });
      page.on("websocket", (ws) => {
        ws.on("framereceived", (frame) => {
          const payload = String(frame.payload);
          if (payload.includes("matchmaking.match.offered")) {
            sockets.get(page)?.push(payload);
          }
        });
      });
    }

    // --- one player queues alone, and is watched -------------------------
    // Alone on purpose: nobody can pair with them, so the only thing that
    // could produce a request is the polling policy itself.
    const solo = pages[0];
    const rest = pages.slice(1);
    if (solo === undefined) throw new Error("[e2e] no pages");
    await solo.goto("/play");
    const controls = solo.getByRole("group", { name: /time control/i });
    await expect(controls).toBeVisible();
    await controls.getByText("1+0", { exact: true }).click();
    await solo.getByRole("button", { name: /join the queue/i }).click();
    await expect(solo.getByText(/searching for an opponent/i)).toBeVisible();

    // Degraded-mode line is absent while the socket is healthy — §17's
    // "no noisy connected banners".
    await expect(solo.getByText(/live updates are unavailable/i)).toHaveCount(0);

    counts.set(solo, 0);
    await solo.waitForTimeout(OBSERVATION_MS);
    const whileRealtime = counts.get(solo) ?? 0;

    console.log(
      `[measure] matchmaking GETs in ${OBSERVATION_MS}ms, realtime: ${whileRealtime}`,
    );
    expect(whileRealtime).toBeLessThanOrEqual(MAX_REQUESTS_WHILE_REALTIME);

    // --- the other two queue, and one of them pairs with the solo player -
    for (const page of rest) {
      await page.goto("/play");
      const theirControls = page.getByRole("group", { name: /time control/i });
      await expect(theirControls).toBeVisible();
      await theirControls.getByText("1+0", { exact: true }).click();
      await page.getByRole("button", { name: /join the queue/i }).click();
    }

    // The offer must arrive **within the push window**, not within the
    // safety interval. Fifteen seconds is comfortably under the
    // twenty-five-second backstop, so passing this proves the socket
    // delivered it rather than the poll noticing.
    const paired = await twoWithAnOffer(pages, 15_000);

    // And it genuinely came over the socket on at least one of them.
    const pushed = paired.some((page) => (sockets.get(page)?.length ?? 0) > 0);
    expect(pushed).toBe(true);

    // --- the handoff still works ----------------------------------------
    for (const page of paired) {
      await page
        .getByRole("alertdialog")
        .getByRole("button", { name: /^accept/i })
        .click();
    }
    for (const page of paired) {
      await expect(page).toHaveURL(/\/games\/[0-9a-f-]{36}$/, { timeout: 30_000 });
      await expect(page.getByRole("grid", { name: /draughts board/i })).toBeVisible({
        timeout: 30_000,
      });
    }

    // --- the draw-state frame replaces the snapshot workaround ----------
    // The offerer is the player without the move, so one opponent move
    // satisfies the re-offer rule — the same arrangement
    // `game-controls.spec.ts` uses, and here the point is that eligibility
    // returns from `game.draw.state` rather than from a re-read snapshot.
    const [first, second] = paired;
    const responder = (await hasTurn(first)) ? first : second;
    const offerer = responder === first ? second : first;

    await offerer.getByRole("button", { name: /offer a draw/i }).click();
    await expect(responder.getByRole("button", { name: /accept draw/i })).toBeVisible({
      timeout: 30_000,
    });
    await responder.getByRole("button", { name: /decline draw/i }).click();
    await expect(offerer.getByRole("button", { name: /offer a draw/i })).toBeDisabled({
      timeout: 30_000,
    });

    const beforeMove = counts.get(offerer) ?? 0;
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

    // Eligibility returns, and it returned **on a frame**: no HTTP read
    // happened on the offerer's page between the decline and this, which is
    // what says the snapshot-per-ply workaround is gone.
    await expect(offerer.getByRole("button", { name: /offer a draw/i })).toBeEnabled({
      timeout: 30_000,
    });
    expect(counts.get(offerer) ?? 0).toBe(beforeMove);
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

async function twoWithAnOffer(pages: Page[], timeoutMs: number): Promise<[Page, Page]> {
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
    await pages[0]?.waitForTimeout(250);
  }
}
