import { type APIRequestContext, expect, test } from "@playwright/test";

import {
  API,
  E2E_ACCOUNTS,
  resetRelationship,
  saveState,
  seededAccount,
  statePath,
} from "./accounts";

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
    // **Retried, the way a player would.** The preview server is shared
    // with eight other projects, and a single dropped request during the
    // session bootstrap leaves the app in its `unavailable` state — "we
    // could not check your session", with a Try again button. That is
    // correct behaviour rather than a bug, and a spec that navigated once
    // and asserted would fail on it. Observed once in a full-suite run.
    await expect(async () => {
      await page.goto("/notifications");
      await expect(page.getByRole("heading", { level: 1, name: "Notifications" })).toBeVisible({
        timeout: 5_000,
      });
    }).toPass({ timeout: 30_000 });

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
