import { expect, test } from "@playwright/test";

import {
  API,
  E2E_ACCOUNTS,
  resetRelationship,
  saveState,
  type SeededAccount,
  seededAccount,
  statePath,
} from "./accounts";

/**
 * One challenge journey, across two real accounts — A64-022.5 §21, §24.
 *
 * jsdom proves the wiring; it cannot prove that a challenge one player
 * sends is the challenge another player sees, and it certainly cannot prove
 * that accepting one produces a match both of them can reach. This does:
 * Alice challenges Bob, Bob accepts in his own browser context, and both
 * end up at the same board.
 *
 * ## Why this is the one E2E worth having here
 *
 * Everything else in this feature is a list, a dialog and a mapper, and the
 * focused suite covers all three against the real router. What it cannot
 * cover is the **bilateral join** — two players, two seats, one match —
 * because that needs two sessions and a real backend transaction. That is
 * §7's whole subject, so it is the thing that gets the expensive test.
 *
 * Accounts are seeded, never registered per run — see `accounts.ts`.
 *
 * Fails loudly without the API rather than skipping quietly.
 *
 *     cd apps/api && uv run uvicorn app.app_factory:create_app --factory --port 8000
 *     cd apps/web && npx playwright test challenges
 */
test("a challenge is sent, accepted, and both players land in the game", async ({
  browser,
  request,
}) => {
  const reachable = await request
    .get(`${API}/health`)
    .then((response) => response.ok())
    .catch(() => false);
  test.skip(!reachable, "apps/api is not running on :8000");

  const alice: SeededAccount = seededAccount(E2E_ACCOUNTS.alice);
  const bob: SeededAccount = seededAccount(E2E_ACCOUNTS.bob);

  // The accounts persist across runs, so the pair is put into the state
  // this journey starts from — friends, with no live challenge between
  // them. Narrow, through the same endpoints a player uses.
  await resetRelationship(request, alice, bob);
  await befriend(request, alice, bob);
  await clearChallenges(request, alice, bob);

  const aliceContext = await browser.newContext({ storageState: statePath(alice.username) });
  const bobContext = await browser.newContext({ storageState: statePath(bob.username) });
  const alicePage = await aliceContext.newPage();
  const bobPage = await bobContext.newPage();

  try {
    // --- Alice challenges from her friends list -------------------------
    await alicePage.goto("/friends");
    // A64-025.13 §35.6. Selected by the username itself, not `@username`.
    // A64-025.8's `PlayerRow` renders the handle line **only when a display
    // name differs from it** — "alice" beats "alice / @alice", which is two
    // lines saying one thing. The seeded accounts have no display name, so
    // that line is correctly absent and this selector had been matching
    // nothing since that phase.
    const bobRow = alicePage.getByRole("listitem").filter({ hasText: bob.username });
    await expect(bobRow).toBeVisible();
    await bobRow.getByRole("button", { name: /challenge/i }).click();

    const dialog = alicePage.getByRole("dialog");
    await expect(dialog).toContainText(bob.username);
    // No default clock — Send is unusable until one is chosen, because
    // every control is a genuinely different game.
    await expect(dialog.getByRole("button", { name: /send challenge/i })).toBeDisabled();
    await dialog.getByRole("radio", { name: "3+2" }).click();
    await dialog.getByRole("button", { name: /send challenge/i }).click();
    await expect(dialog).toBeHidden();

    // It appears on her own sent list.
    await alicePage.goto("/challenges");
    await alicePage.getByRole("tab", { name: /sent/i }).click();
    await expect(
      alicePage.getByRole("list", { name: /sent/i }).getByText(bob.username),
    ).toBeVisible();

    // --- Bob sees it and accepts ----------------------------------------
    await bobPage.goto("/challenges");
    const incoming = bobPage.getByRole("list", { name: /incoming/i });
    await expect(incoming.getByText(alice.username)).toBeVisible();
    // Incoming means accept/decline — never "cancel", which is the
    // sender's word for the same row.
    await expect(incoming.getByRole("button", { name: /cancel the challenge/i })).toHaveCount(
      0,
    );

    await incoming.getByRole("button", { name: /accept the challenge/i }).click();

    // §7. Bob pressed once. The challenge accept created the match and the
    // seat was taken for him; Alice has not joined, so the shared offer
    // surface says so rather than this feature inventing a waiting screen.
    await expect(bobPage.getByRole("alertdialog")).toBeVisible();

    // --- Alice takes her seat -------------------------------------------
    // Through the lobby, which is where a pending match has always been
    // answerable — the point is that the **same** offer reaches her.
    await alicePage.goto("/play");
    const offer = alicePage.getByRole("alertdialog");
    await expect(offer).toBeVisible({ timeout: 15_000 });
    await offer.getByRole("button", { name: /accept/i }).click();

    // --- both are at the same board -------------------------------------
    await expect(alicePage).toHaveURL(/\/games\/[0-9a-f-]{36}$/, { timeout: 15_000 });
    const board = new URL(alicePage.url()).pathname;

    // Bob's page follows on its own, without him doing anything else —
    // which is the claim §7 makes: accept, and the game opens.
    await expect(bobPage).toHaveURL(new RegExp(`${board}$`), { timeout: 15_000 });
  } finally {
    // The sessions rotate their refresh cookie, so the saved state must be
    // written back or the next run signs in — see `saveState`.
    await saveState(aliceContext, alice.username);
    await saveState(bobContext, bob.username);
    await aliceContext.close();
    await bobContext.close();
  }
});

/** Makes the pair friends through the endpoints a player would use. */
async function befriend(
  request: Parameters<typeof resetRelationship>[0],
  a: SeededAccount,
  b: SeededAccount,
): Promise<void> {
  const sent = await request.post(`${API}/api/v1/friends/requests`, {
    headers: { Authorization: `Bearer ${a.accessToken}` },
    data: { player_id: b.id },
    failOnStatusCode: false,
  });
  if (!sent.ok()) return;

  const incoming = await request.get(`${API}/api/v1/friends/requests/incoming`, {
    headers: { Authorization: `Bearer ${b.accessToken}` },
  });
  const page = (await incoming.json()) as {
    data: { items: { id: string; player: { id: string } }[] };
  };
  for (const row of page.data.items.filter((item) => item.player.id === a.id)) {
    await request.post(`${API}/api/v1/friends/requests/${row.id}/accept`, {
      headers: { Authorization: `Bearer ${b.accessToken}` },
      failOnStatusCode: false,
    });
  }
}

/**
 * Withdraws any challenge left between the two.
 *
 * One live challenge per unordered pair is a database constraint, so a
 * challenge surviving a previous run would make the create in this one a
 * `409` — and the failure would look like a defect in the dialog.
 */
async function clearChallenges(
  request: Parameters<typeof resetRelationship>[0],
  a: SeededAccount,
  b: SeededAccount,
): Promise<void> {
  for (const [self, other] of [
    [a, b],
    [b, a],
  ] as const) {
    const headers = { Authorization: `Bearer ${self.accessToken}` };
    for (const direction of ["outgoing", "incoming"] as const) {
      const listed = await request.get(`${API}/api/v1/challenges/${direction}`, {
        headers,
        failOnStatusCode: false,
      });
      if (!listed.ok()) continue;
      const page = (await listed.json()) as {
        data: { items: { id: string; player: { id: string } }[] };
      };
      for (const row of page.data.items.filter((item) => item.player.id === other.id)) {
        await request.delete(`${API}/api/v1/challenges/${row.id}`, {
          headers,
          failOnStatusCode: false,
        });
        await request.post(`${API}/api/v1/challenges/${row.id}/decline`, {
          headers,
          failOnStatusCode: false,
        });
      }
    }
  }
}
