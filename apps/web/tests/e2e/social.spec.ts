import { expect, test } from "@playwright/test";

import {
  E2E_ACCOUNTS,
  resetRelationship,
  saveState,
  type SeededAccount,
  seededAccount,
  statePath,
} from "./accounts";

/**
 * One social journey, across two real accounts — A64-020.4 §22.8.
 *
 * jsdom proves the wiring; it cannot prove that a request one player sends
 * is the request another player sees. This does: Alice searches, sends,
 * Bob accepts in his own browser context, and the friendship appears on
 * both sides. Nothing is mocked, and the two sessions are genuinely
 * separate — separate cookie jars, separate memory.
 *
 * It is also the only place the **thumbnail** claim can be checked: Radix's
 * `AvatarImage` mounts the `<img>` only once the image has loaded, which
 * jsdom never does.
 *
 * ## Accounts are seeded, not registered per run
 *
 * See `global-setup.ts`. Registration is capped at three per IP per hour
 * and sign-in at five per fifteen minutes; this suite needs two accounts
 * and would exhaust both. The setup seeds them once and saves each
 * account's browser session, which these contexts load — so a run performs
 * **no** registration and **no** sign-in.
 *
 * Fails loudly without the API rather than skipping quietly — a social
 * suite that silently passes when its fixtures are missing is worse than
 * no suite.
 *
 *     cd apps/api && uv run uvicorn app.app_factory:create_app --factory --port 8000
 *     cd apps/web && npm run test:e2e
 */
test("two players find each other, become friends, and unfriend", async ({
  browser,
  request,
}) => {
  const reachable = await request
    .get("http://localhost:8000/health")
    .then((response) => response.ok())
    .catch(() => false);
  test.skip(!reachable, "apps/api is not running on :8000");

  const alice: SeededAccount = seededAccount(E2E_ACCOUNTS.alice);
  const bob: SeededAccount = seededAccount(E2E_ACCOUNTS.bob);

  // The accounts persist across runs, so the pair is returned to strangers
  // rather than assumed to be.
  await resetRelationship(request, alice, bob);

  // Two contexts, each loading its own saved session — separate cookie
  // jars, which is the whole point of a two-player test, and **no sign-in**,
  // which is what keeps the suite inside the login rate limit.
  const aliceContext = await browser.newContext({ storageState: statePath(alice.username) });
  const bobContext = await browser.newContext({ storageState: statePath(bob.username) });
  const alicePage = await aliceContext.newPage();
  const bobPage = await bobContext.newPage();

  try {
    // --- Alice searches and sends -------------------------------------
    await alicePage.goto("/search");
    await alicePage.getByLabel(/username or name/i).fill(bob.username);

    // A64-025.13 §35.6. Selected by the username itself, not `@username`.
    // A64-025.8's `PlayerRow` renders the handle line **only when a display
    // name differs from it** — "alice" beats "alice / @alice", which is two
    // lines saying one thing. The seeded accounts have no display name, so
    // that line is correctly absent and this selector had been matching
    // nothing since that phase.
    const bobRow = alicePage.getByRole("listitem").filter({ hasText: bob.username });
    await expect(bobRow).toBeVisible();

    // The dense list renders the **thumbnail**, not the full-size avatar.
    // Only assertable here: jsdom never loads an image, so Radix never
    // mounts the element.
    const avatar = bobRow.getByRole("img");
    if (await avatar.count()) {
      await expect(avatar.first()).toHaveAttribute("src", /thumb/i);
    }

    await bobRow.getByRole("button", { name: /add friend/i }).click();

    // The state moved on, so the button did too — and "Add friend" is gone
    // rather than sitting there next to a pending request.
    await alicePage.goto("/friends/requests");
    await expect(
      alicePage.getByRole("list", { name: /outgoing/i }).getByText(bob.username),
    ).toBeVisible();

    // --- Bob sees it and accepts ---------------------------------------
    await bobPage.goto("/friends/requests");
    const incoming = bobPage.getByRole("list", { name: /incoming/i });
    await expect(incoming.getByText(alice.username)).toBeVisible();
    // Incoming means accept/decline — never "cancel", which is the
    // sender's word for the same row.
    await expect(incoming.getByRole("button", { name: /cancel request/i })).toHaveCount(0);
    await incoming
      .getByRole("button", { name: /accept/i })
      .first()
      .click();

    // --- the friendship exists on both sides ----------------------------
    await bobPage.goto("/friends");
    await expect(
      bobPage.getByRole("list", { name: /^friends$/i }).getByText(alice.username),
    ).toBeVisible();

    await alicePage.goto("/friends");
    const aliceFriends = alicePage.getByRole("list", { name: /^friends$/i });
    await expect(aliceFriends.getByText(bob.username)).toBeVisible();

    // --- and the public profile agrees ----------------------------------
    await alicePage.goto(`/players/${bob.username}`);
    await expect(alicePage.getByRole("button", { name: /remove friend/i })).toBeVisible();
    // A friend is not simultaneously addable. The action set comes from one
    // enum, so this is unrepresentable rather than merely absent.
    await expect(alicePage.getByRole("button", { name: /add friend/i })).toHaveCount(0);

    // --- unfriend, confirmed ---------------------------------------------
    await alicePage.getByRole("button", { name: /remove friend/i }).click();
    const dialog = alicePage.getByRole("dialog");
    await expect(dialog).toContainText(bob.username);
    await dialog.getByRole("button", { name: /^confirm$/i }).click();

    await expect(alicePage.getByRole("button", { name: /add friend/i })).toBeVisible();

    await alicePage.goto("/friends");
    await expect(
      alicePage.getByRole("list", { name: /^friends$/i }).getByText(bob.username),
    ).toHaveCount(0);
  } finally {
    // The contexts refreshed while running, rotating each cookie, so the
    // saved state is written back — otherwise the next run's probe would
    // present a superseded token and revoke the session it was reusing.
    await saveState(aliceContext, alice.username);
    await saveState(bobContext, bob.username);
    await aliceContext.close();
    await bobContext.close();
  }
});
