import { type APIRequestContext, expect, test } from "@playwright/test";

import {
  API,
  E2E_ACCOUNTS,
  resetRelationship,
  saveState,
  seededAccount,
  statePath,
} from "./accounts";
import { gotoBooted, reloadBooted } from "./session";

/**
 * One notification, from the fact that caused it to the page it leads to —
 * A64-021.1 §32.10.
 *
 * jsdom proves the wiring. It cannot prove that a friend request one player
 * sends becomes a durable row another player finds in their own browser,
 * because everything in between is a real outbox, a real relay and a real
 * database. This does.
 *
 * The journey:
 *
 *     Alice sends a friend request        through the API — the sending is
 *                                         not the subject, `social.spec.ts`
 *                                         already covers it through the UI
 *     the relay runs                      on its own, in the API process
 *     Bob opens /notifications            his own browser context
 *     Bob follows the notification        to the list where he can answer it
 *     the unread count falls back         reconciled against the server
 *
 * ## Why the arrival is awaited through the API rather than by reloading
 *
 * The relay polls once a second, so the row appears a moment after the
 * request. Waiting for it in the UI would mean reloading in a loop and
 * asserting on an empty list in between — a spec that is mostly retry
 * scaffolding. Polling the API until the notification exists makes the
 * *browser* half of the test deterministic, which is the half worth
 * asserting.
 *
 * ## Why the spec clears the account first
 *
 * Notifications are append-only (§14), so Bob carries whatever every
 * earlier run produced. An earlier version measured a baseline and asserted
 * a delta, and that baseline is exactly the thing another run can change
 * underneath it — which it did, and reported as a notification that never
 * arrived.
 *
 * Marking everything read through the endpoint under test costs one request
 * and makes every count below **exact**: nought, then one, then nought.
 *
 * Fails loudly without the API rather than skipping quietly.
 *
 *     cd apps/api && uv run uvicorn main:app --port 8000
 *     cd apps/web && npm run test:e2e
 */
// **Serial**, because both tests drive the same two seeded accounts.
// `fullyParallel` spreads tests across workers as readily as files, and two
// workers resetting Alice and Bob's relationship at once would make each
// test fail for something the other did.
test.describe.configure({ mode: "serial" });

test("a friend request notifies its recipient, who reads it and follows it", async ({
  browser,
  request,
}) => {
  // **Four times the default.** Everything this spec waits for is
  // asynchronous by design: the relay polls once a second, and under a full
  // suite run it is draining game and matchmaking events alongside this
  // one. The default thirty seconds is smaller than the two polls below
  // are allowed to take, so the *test* deadline expired before the polls
  // did — which reported as "expected 2, received 1" and looked like a
  // missing notification rather than a budget.
  test.setTimeout(120_000);
  const alice = seededAccount(E2E_ACCOUNTS.alice);
  const bob = seededAccount(E2E_ACCOUNTS.bob);
  const bobAuth = { Authorization: `Bearer ${bob.accessToken}` };

  // A previous run may have left these two friends, or left a pending
  // request. Narrow, through the same endpoints a player uses.
  await resetRelationship(request, alice, bob);

  // **Start from "all caught up".** Notifications are append-only (§14), so
  // this account carries whatever every earlier run produced — and a spec
  // that measured a baseline and asserted a delta was measuring a number
  // another failed run could change underneath it. Marking everything read
  // through the endpoint under test puts Bob in a state a player can
  // genuinely be in, and makes every count below exact rather than relative.
  const cleared = await request.post(`${API}/api/v1/notifications/read-all`, {
    headers: bobAuth,
  });
  expect(cleared.status(), await cleared.text()).toBe(200);
  expect(await unreadTotal(request, bobAuth)).toBe(0);

  const sent = await request.post(`${API}/api/v1/friends/requests`, {
    headers: { Authorization: `Bearer ${alice.accessToken}` },
    data: { player_id: bob.id },
  });
  expect(sent.status(), await sent.text()).toBe(201);
  const requestId = ((await sent.json()) as { data: { id: string } }).data.id;

  // The relay turns the outbox row into a durable notification. Nothing on
  // the request path did this — the HTTP call above returned before any of
  // it happened, which is the whole point of an outbox.
  await expect.poll(() => unreadTotal(request, bobAuth), { timeout: 45_000 }).toBe(1);

  const context = await browser.newContext({ storageState: statePath(E2E_ACCOUNTS.bob) });
  const page = await context.newPage();

  try {
    // Retried the way a player would — see `gotoBooted`.
    await gotoBooted(page, "/notifications");

    // The message is assembled in the browser from a type and an actor — no
    // sentence was stored, which is what makes it readable in three
    // languages. A fresh context is English.
    const row = page
      .getByRole("listitem")
      .filter({ hasText: `${alice.username} sent you a friend request` })
      .first();
    await expect(row).toBeVisible();
    // §28: unread is a word as well as a dot.
    await expect(row.getByText("Unread")).toBeAttached();

    // §18, §28: the badge names its count in words, not only in a circle —
    // and it is exactly one, because the account started caught up.
    await expect(page.getByRole("link", { name: "Notifications — 1 unread" })).toBeVisible();

    // §32.10's last step: the target navigates, to the list where a received
    // request can actually be answered.
    await row.getByRole("link").click();
    await expect(page).toHaveURL(/\/friends\/requests$/);

    // Marking happened alongside the navigation and is reconciled against
    // the server: the one notification that arrived is now read, so Bob is
    // caught up again.
    await expect.poll(() => unreadTotal(request, bobAuth), { timeout: 15_000 }).toBe(0);
  } finally {
    // **Cancelled by id, unconditionally.** Alice and Bob are shared with
    // `social.spec.ts`, which sends a request of its own — and a pending
    // one left behind makes that send a `409` and that suite fail for a
    // reason nothing in its file mentions. Found exactly that way.
    //
    // By id rather than through `resetRelationship`: this spec knows which
    // request it created, and a cleanup that has to search for its own
    // leftovers is a cleanup that can miss them.
    await request.delete(`${API}/api/v1/friends/requests/${requestId}`, {
      headers: { Authorization: `Bearer ${alice.accessToken}` },
      failOnStatusCode: false,
    });

    // The refresh cookie rotates on every use; a context that did not save
    // it back leaves the next run with a superseded token.
    await saveState(context, E2E_ACCOUNTS.bob);
    await context.close();
  }
});

/** Bob's unread count, straight from the endpoint the badge reads. */
async function unreadTotal(request: APIRequestContext, headers: Record<string, string>) {
  const counted = await request.get(`${API}/api/v1/notifications/unread-count`, { headers });
  if (!counted.ok()) return -1;
  const body = (await counted.json()) as { data: { unread_count: number } };
  return body.data.unread_count;
}

/**
 * The realtime half — A64-021.2 §11.
 *
 * Bob is looking at his notifications. Alice sends him a friend request. His
 * page updates **on its own** — no reload, no focus event, no poll — because
 * the frame arrived on the socket he already had open and the app re-read.
 *
 * The last step is what makes the whole design honest: a reload shows the
 * *same* state. The frame did not create anything on the client; it caused a
 * read, and the read is what the page was already showing (§5).
 *
 * ## One browser, and why that is not a shortcut
 *
 * §11 describes two browsers, with Alice sending through hers. This sends
 * through the API instead, and the reason is a demonstrated failure rather
 * than convenience: `social.spec.ts` runs as this project's dependency and
 * drives Alice's *browser session*, and a second context loading the same
 * saved session in the same run left her signed out — the refresh-token
 * rotation hazard `playwright.config.ts` documents at length. Three runs
 * failed that way, on a page that was correct.
 *
 * Nothing about the property under test moves. Alice's browser would call
 * exactly this endpoint, `social.spec.ts` already covers her half of the
 * journey through the UI, and every claim §11 makes — an open page updating
 * with no refresh, the badge, the list, and a reload agreeing — is on Bob's
 * side and is asserted below in a real browser.
 */
test("a notification reaches an open page with no refresh, and a reload agrees", async ({
  browser,
  request,
}) => {
  // The relay is asynchronous and the fleet fan-out is one more hop, so the
  // same budget the durable journey needs applies here.
  test.setTimeout(120_000);

  const alice = seededAccount(E2E_ACCOUNTS.alice);
  const bob = seededAccount(E2E_ACCOUNTS.bob);
  const bobAuth = { Authorization: `Bearer ${bob.accessToken}` };

  await resetRelationship(request, alice, bob);
  const cleared = await request.post(`${API}/api/v1/notifications/read-all`, {
    headers: bobAuth,
  });
  expect(cleared.status(), await cleared.text()).toBe(200);

  const context = await browser.newContext({ storageState: statePath(E2E_ACCOUNTS.bob) });
  const page = await context.newPage();
  let requestId: string | null = null;

  try {
    // **Bob is looking at his notifications before anything happens.** That
    // ordering is the test: everything below has to reach a page that is
    // already rendered and is never navigated again until the reload.
    await gotoBooted(page, "/notifications");

    // Caught up: the bell names no count. **Not** the empty state —
    // notifications are append-only (A64-021.1 §14), so this account keeps
    // whatever earlier runs produced and "you are all caught up" is only
    // ever true of an account with no history at all. The badge is the
    // assertion that holds on the first run and on the hundredth.
    await expect(page.getByRole("link", { name: "Notifications" })).toBeVisible();
    await expect(page.getByRole("link", { name: /Notifications — \d+ unread/ })).toHaveCount(0);

    const sent = await request.post(`${API}/api/v1/friends/requests`, {
      headers: { Authorization: `Bearer ${alice.accessToken}` },
      data: { player_id: bob.id },
    });
    expect(sent.status(), await sent.text()).toBe(201);
    requestId = ((await sent.json()) as { data: { id: string } }).data.id;

    // §11: the badge updates with **no refresh**. `toBeVisible` polls the
    // DOM, never the network — nothing here reloads or refocuses the page,
    // and the query it renders from has a ten-second stale time it is
    // nowhere near exceeding.
    await expect(page.getByRole("link", { name: "Notifications — 1 unread" })).toBeVisible({
      timeout: 60_000,
    });

    // And so did the page under it: the list invalidated, refetched, and
    // rendered the row. Newest first, and the only unread one — everything
    // older was marked read above.
    const newest = page.getByRole("listitem").first();
    await expect(newest).toContainText(`${alice.username} sent you a friend request`);
    await expect(newest.getByText("Unread")).toBeAttached();

    // §5's whole point, asserted the only way it can be: a reload shows the
    // **same** state. If the frame had mutated the UI rather than causing a
    // read, this is where the two would disagree.
    // Reloaded, not re-navigated: the claim is that this state *survives*
    // a reload, so recovering by going somewhere else would prove nothing.
    await reloadBooted(page);
    await expect(page.getByRole("link", { name: "Notifications — 1 unread" })).toBeVisible();
    await expect(page.getByRole("listitem").first()).toContainText(
      `${alice.username} sent you a friend request`,
    );
    await expect(page.getByRole("listitem").first().getByText("Unread")).toBeAttached();
  } finally {
    if (requestId !== null) {
      await request.delete(`${API}/api/v1/friends/requests/${requestId}`, {
        headers: { Authorization: `Bearer ${alice.accessToken}` },
        failOnStatusCode: false,
      });
    }
    await request.post(`${API}/api/v1/notifications/read-all`, { headers: bobAuth });
    await saveState(context, E2E_ACCOUNTS.bob);
    await context.close();
  }
});
