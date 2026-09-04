import { execFile } from "node:child_process";
import { resolve } from "node:path";
import { promisify } from "node:util";

import { expect, test } from "@playwright/test";

import { E2E_ACCOUNTS, resetLobby, saveState, seededAccount, statePath } from "./accounts";

/**
 * The tournament flow, against the real backend — A64-020.6 §28, §29.10.
 *
 * List → detail → enter → participant state → withdraw, in a browser,
 * through the real API.
 *
 * ## Where the tournament comes from
 *
 * `python -m app.operator.tournament`, the repository's **existing**
 * operator entry point. §28 allows exactly this — "supported admin/operator
 * setup only if the repository already has one" — and it is the only path
 * that exists: creating and opening a tournament are deliberately not HTTP,
 * because this platform has no administrator role and an endpoint behind
 * `CurrentUser` would let every registered player open one.
 *
 * Nothing here truncates a table, flushes Redis, disables a rate limit or
 * adds a browser-reachable backdoor. The spec drives the same two player
 * endpoints a person would, and creates its fixture through the same
 * command an operator would type.
 *
 * ## What this deliberately does not cover, and why
 *
 * A **completed** tournament's standings. Reaching one from here means
 * playing a whole bracket to its final — four accounts, three matches, and
 * a wait on the clock worker — which is minutes of wall time for a table
 * whose contents are already asserted twice: by
 * `tests/contract/test_tournament_results.py` against a real bracket
 * played out through the production services, and by
 * `tournament.test.tsx` against the real router for the rendering. §28
 * asks for the highest-value stable path when one flow cannot cover both;
 * this is that path, and this paragraph is the documented gap.
 */
const run = promisify(execFile);

/** The API directory, from `apps/web`. */
const API_DIR = resolve(process.cwd(), "..", "api");

test.setTimeout(120_000);

async function operator(...args: string[]): Promise<string> {
  const { stdout } = await run(
    "uv",
    ["run", "python", "-m", "app.operator.tournament", ...args],
    {
      cwd: API_DIR,
    },
  );
  return stdout.trim();
}

test("a player finds a tournament, enters it, and withdraws again", async ({
  browser,
  request,
}) => {
  const reachable = await request
    .get("http://localhost:8000/health")
    .then((response) => response.ok())
    .catch(() => false);
  test.skip(!reachable, "apps/api is not running on :8000");

  // A fresh tournament per run, open for entry. Named so a human reading
  // the database can tell where it came from.
  const created = await operator(
    "create",
    "--name",
    "E2E Open",
    "--capacity",
    "8",
    "--variant",
    "russian_8x8",
  );
  const tournamentId = /created ([0-9a-f-]{36})/.exec(created)?.[1];
  expect(tournamentId, `unexpected operator output: ${created}`).toBeTruthy();
  await operator("open", tournamentId as string);

  // **The lobby trio's spare, at the end of their chain** — A64-020.6 §28.
  //
  // Borrowing `e2e_profile_owner` was tried and failed exactly as
  // `playwright.config.ts` predicts: run beside `profile.spec.ts`, the two
  // contexts refreshed one session, the loser presented a superseded token,
  // and the server revoked the whole chain — both specs failing for a
  // reason neither file mentions.
  //
  // A seventh seeded account was the other option and costs a registration,
  // which is three per IP per hour and already spoken for by `auth.spec.ts`.
  // This spec never queues and never plays, so once the game chain has
  // finished with `e2e_lobby_three` nothing else touches it.
  const username = E2E_ACCOUNTS.lobbyThree;

  // **Out of any live game first.** The comment above says nothing touches
  // this account once the game chain is done, and that is true — but what
  // the chain *leaves behind* is not nothing: `realtime-push.spec.ts` ends
  // with a match still running, and since A64-020.5A the lobby sends a
  // player in a live game to the board rather than to the queue form. So
  // `/play` answered with a redirect to `/games/{id}` and this spec failed
  // looking for a navigation link on a page it was never on.
  //
  // `resetLobby` is the helper the four game specs already use for exactly
  // this, and it waits rather than reaching for a back door — `bullet_1_0`
  // flags sixty seconds after activation and there is no resign endpoint to
  // call. This spec neither queues nor plays, so the wait is the whole cost.
  await resetLobby(request, seededAccount(username));

  const context = await browser.newContext({ storageState: statePath(username) });

  try {
    const page = await context.newPage();

    // Count the reads: one per surface, and **none** per participant.
    let bracketRequests = 0;
    let profileRequests = 0;
    page.on("request", (req) => {
      if (req.method() !== "GET") return;
      const url = req.url();
      if (url.includes(`/tournaments/${tournamentId}/bracket`)) bracketRequests += 1;
      if (/\/api\/v1\/(users|profiles)\//.test(url)) profileRequests += 1;
    });

    // --- the lobby, reached from the shell's own navigation --------------
    await page.goto("/play");
    await page.getByRole("link", { name: /tournaments|turnirlar/i }).click();
    await expect(page).toHaveURL(/\/tournaments$/);

    const list = page.getByRole("list", { name: /tournaments|turnirlar/i });
    await expect(list).toBeVisible({ timeout: 30_000 });

    // --- narrowed by the **server**, then opened -------------------------
    await page.getByRole("radio", { name: /registration open|ro'yxat ochiq/i }).click();
    await list
      .getByRole("link", { name: /E2E Open/ })
      .first()
      .click();
    await expect(page).toHaveURL(new RegExp(`/tournaments/${tournamentId}$`));

    // --- the authoritative participant state, before anything -----------
    await expect(page.getByText(/you have not entered|yozilmagansiz/i)).toBeVisible({
      timeout: 30_000,
    });

    // --- enter, and read back what the server confirmed -------------------
    await page.getByRole("button", { name: /enter tournament|turnirga yozilish/i }).click();
    await expect(page.getByText(/you are entered|siz yozilgansiz/i)).toBeVisible({
      timeout: 30_000,
    });

    // The entrant count moved, which is the detail cache having been
    // invalidated rather than a stale number left on screen.
    await expect(page.getByText(/1 of 8|8 tadan 1 ta/i)).toBeVisible();

    // --- withdraw, through the confirmation ------------------------------
    await page.getByRole("button", { name: /^withdraw$|^chiqish$/i }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: /yes, withdraw|ha, chiqaman/i }).click();

    await expect(page.getByText(/you withdrew|turnirdan chiqdingiz/i)).toBeVisible({
      timeout: 30_000,
    });
    // Withdrawal is not a deletion: the entry survives as `withdrawn`, and
    // re-entry is offered because registration is still open.
    await expect(
      page.getByRole("button", { name: /enter tournament|turnirga yozilish/i }),
    ).toBeVisible();

    // One bracket read for the page — the tournament is not in progress, so
    // nothing polls it — and none of the identity lookups §26 forbids.
    expect(bracketRequests).toBe(1);
    expect(profileRequests).toBe(0);

    // A tournament nobody has seeded has no bracket yet, and that is a
    // state the page renders rather than an error.
    await expect(
      page.getByText(/drawn once registration closes|ro'yxat yopilgach/i),
    ).toBeVisible();
  } finally {
    await saveState(context, username);
    await context.close();
  }
});

test("a visitor with no account reads a tournament and is asked to sign in to enter", async ({
  browser,
  request,
}) => {
  // A64-026.4 §43.5, end to end and through the real API — the half the
  // component tests substitute. What is asserted here is that the *server*
  // answers a browser carrying no cookie at all, which a mocked HTTP layer
  // cannot tell you.
  const reachable = await request
    .get("http://localhost:8000/health")
    .then((response) => response.ok())
    .catch(() => false);
  test.skip(!reachable, "apps/api is not running on :8000");

  const created = await operator(
    "create",
    "--name",
    "E2E Public",
    "--capacity",
    "8",
    "--variant",
    "russian_8x8",
  );
  const tournamentId = /created ([0-9a-f-]{36})/.exec(created)?.[1];
  expect(tournamentId, `unexpected operator output: ${created}`).toBeTruthy();
  await operator("open", tournamentId as string);

  // No `storageState`: a genuinely fresh browser, which is the whole point.
  const context = await browser.newContext();

  try {
    const page = await context.newPage();

    await page.goto("/tournaments");
    await expect(page).toHaveURL(/\/tournaments$/);
    await expect(page.getByText("E2E Public").first()).toBeVisible();

    await page.goto(`/tournaments/${tournamentId as string}`);
    // Not redirected to sign-in, which is what the guard used to do.
    await expect(page).toHaveURL(new RegExp(`/tournaments/${tournamentId as string}$`));
    await expect(page.getByRole("heading", { name: "E2E Public" })).toBeVisible();

    // Reading is open; entering is not, and the deep link survives it.
    const cta = page.getByRole("link", { name: /sign in to enter/i });
    await expect(cta).toBeVisible();
    await expect(cta).toHaveAttribute("href", new RegExp(tournamentId as string));

    // The guarded routes are still guarded, from the same fresh context.
    await page.goto("/play");
    await expect(page).toHaveURL(/\/login/);
  } finally {
    await context.close();
  }
});
