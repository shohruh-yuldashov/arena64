import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import type { APIRequestContext } from "@playwright/test";

/**
 * The seeded E2E accounts, and how a spec uses them — A64-020.4 §21.
 *
 * `global-setup.ts` creates them once and saves a **browser session** per
 * account. A spec loads that session as `storageState` and never signs in,
 * so a run costs nothing against either rate limit:
 *
 *     register   3 per IP per hour
 *     login      5 per IP per 15 minutes
 *
 * A64-020.3 registered a fresh account per run and became unrunnable on the
 * fourth. A social suite needs two accounts, so it would have hit the
 * register cap on its second run and the login cap on its first.
 *
 * The trade is that the accounts **accumulate state**: a friendship from
 * one run is still there on the next. Specs therefore call
 * `resetRelationship` rather than assuming a clean slate — narrow, through
 * the same endpoints a player uses, and never a truncation.
 */
export const API = process.env.ARENA64_E2E_API ?? "http://localhost:8000";
export const PASSWORD = "CorrectHorse1!";

/**
 * Where the saved sessions live. Ignored by git — they carry a credential.
 *
 * **Not `test-results/`** — A64-020.5A. Playwright clears its output
 * directory at the start of every run, *before* `globalSetup`, so sessions
 * stored there were wiped on every invocation and the setup signed in for
 * every account, every time. The strategy A64-020.4 built to make repeat
 * runs cost nothing was therefore never in effect: it performed five logins
 * a run against a five-per-IP-per-fifteen-minutes budget, which is why it
 * appeared to work and why adding a sixth account broke it outright.
 *
 * A sibling directory Playwright does not manage. Repeat runs now cost
 * **zero** logins, which is what the seeding was for.
 */
export const AUTH_DIR = resolve(process.cwd(), ".auth");

export function statePath(username: string): string {
  return resolve(AUTH_DIR, `${username}.json`);
}

/**
 * Writes a context's rotated cookies back **without losing the account**.
 *
 * `context.storageState({ path })` writes Playwright's own shape —
 * `{ cookies, origins }` — and nothing else. The seeded account block this
 * harness adds beside it is silently dropped, so a spec that saved directly
 * left a file `seededAccount` would then reject as unseeded, and
 * `global-setup` would rebuild only the fragment it could infer.
 *
 * It went unnoticed because the next run's `sessionIsLive` rewrites the
 * file from a *read* of the same file, so a partially-restored `arena64`
 * kept working for the one field `resetRelationship` happened to use.
 *
 * Every spec that loads a seeded session must save through this.
 */
export async function saveState(
  context: { storageState: (options: { path: string }) => Promise<unknown> },
  username: string,
): Promise<void> {
  const path = statePath(username);
  const account = JSON.parse(readFileSync(path, "utf8")) as { arena64?: SeededAccount };
  await context.storageState({ path });
  const rotated = JSON.parse(readFileSync(path, "utf8")) as Record<string, unknown>;
  writeFileSync(path, JSON.stringify({ ...rotated, arena64: account.arena64 }, null, 2));
}

/** Stable, and namespaced so they cannot collide with a real player. */
export const E2E_ACCOUNTS = {
  alice: "e2e_social_alice",
  bob: "e2e_social_bob",
  /** The profile suite's own, so it stops registering per run too. */
  profile: "e2e_profile_owner",
  /**
   * The lobby suite's own pair — A64-020.5A §26.
   *
   * **Not** `alice` and `bob`. Playwright runs spec files in parallel, and
   * a lobby run cancels its accounts' queue tickets and settles their
   * pending offers; doing that to the accounts the social suite is
   * simultaneously friending would make both suites flaky for reasons
   * neither could see from its own file.
   *
   * **Three**, not two. QT-3 excludes a player's most recent opponent and has
   * no time window, so a fixed pair is pairable exactly once ever — the
   * moment they finish a game they block each other permanently. With
   * three, at most one of the three pairs can be excluded, so a pairing is
   * always available and the flow is repeatable.
   */
  lobbyOne: "e2e_lobby_one",
  lobbyTwo: "e2e_lobby_two",
  lobbyThree: "e2e_lobby_three",
} as const;

/**
 * Everything the lobby suite needs cleared before it runs — A64-020.5A §26.
 *
 * Two accounts are about to queue into the same pool, and the state that
 * would break that is state a **previous run** left behind: a live ticket
 * (QT-1 refuses a second, so the join would `409`), or an unanswered match
 * offer (which the lobby would show instead of the form).
 *
 * Both are cleared through the endpoints a player uses. Nothing here
 * truncates a table, flushes Redis or disables a rate limit — §26 forbids
 * all three, and each would make the suite pass by removing the production
 * behaviour it is supposed to run against.
 *
 * Declining is deliberately **not** how a stale offer is cleared: a decline
 * earns a queue cooldown, which is precisely the state that would then stop
 * the spec from queueing. Accepting settles it just as well and costs
 * nothing, because this phase never plays the game.
 *
 * ## An **active** match has to end on its own
 *
 * Since A64-020.5A `GET /matches/pending` reports a game that has started,
 * and the lobby correctly sends that player to it rather than to the queue
 * form — so an account still in yesterday's match cannot join a pool. There
 * is no resign endpoint in this phase and no back door worth building: the
 * only player-facing way an unplayed game ends is the clock running out,
 * which for `bullet_1_0` is sixty seconds after activation and is exactly
 * what the flag worker is for.
 *
 * So this waits, bounded, and says why if it gives up. In practice the wait
 * is zero — two runs are minutes apart and the previous match flagged long
 * ago — and it only costs anything back to back, which is the case that
 * would otherwise fail confusingly.
 *
 * Every call tolerates "already absent". The point is the end state, not
 * the transition — the same contract `resetRelationship` keeps.
 */
export async function resetLobby(
  request: APIRequestContext,
  account: SeededAccount,
): Promise<void> {
  const headers = { Authorization: `Bearer ${account.accessToken}` };

  // The offer first. A pending match holds its tickets, so cancelling a
  // queue that a pairing already consumed would leave the offer standing.
  const pending = await request.get(`${API}/api/v1/matchmaking/matches/pending`, {
    headers,
    failOnStatusCode: false,
  });
  if (pending.ok()) {
    const body = (await pending.json()) as { data: { match_id: string; status: string } };
    if (body.data.status === "pending_acceptance") {
      await request.post(`${API}/api/v1/matchmaking/matches/${body.data.match_id}/accept`, {
        headers,
        failOnStatusCode: false,
      });
    }
  }

  await request.delete(`${API}/api/v1/matchmaking/queue`, {
    headers,
    failOnStatusCode: false,
  });

  await settled(request, headers);
}

/**
 * Waits until this account is not in a live game.
 *
 * `bullet_1_0` flags sixty seconds after activation and the adjudicator
 * runs every second, so ninety is comfortably past the point where a
 * failure means something other than "not yet".
 */
async function settled(
  request: APIRequestContext,
  headers: Record<string, string>,
  timeoutMs = 90_000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const current = await request.get(`${API}/api/v1/matchmaking/matches/pending`, {
      headers,
      failOnStatusCode: false,
    });
    if (!current.ok()) return;

    const body = (await current.json()) as { data: { status: string } };
    if (body.data.status !== "active") return;

    if (Date.now() > deadline) {
      throw new Error(
        "[e2e] an account is still in an active match after " +
          `${timeoutMs}ms — its clock should have flagged. Is the game clock ` +
          "worker running (GAME_CLOCK_ENABLED)?",
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }
}

export interface SeededAccount {
  username: string;
  email: string;
  password: string;
  id: string;
  accessToken: string;
}

/**
 * The account the global setup seeded, read from disk.
 *
 * Throws rather than seeding on demand: a spec that reached this without a
 * setup having run is a suite whose fixtures are broken, and discovering
 * that as a clear error beats discovering it as a mysterious `401`.
 */
export function seededAccount(username: string): SeededAccount {
  const path = statePath(username);
  try {
    const saved = JSON.parse(readFileSync(path, "utf8")) as { arena64?: SeededAccount };
    if (saved.arena64 === undefined) throw new Error("no account in the saved state");
    return saved.arena64;
  } catch (cause) {
    throw new Error(
      `[e2e] ${username} was not seeded — expected ${path}. Is apps/api running?`,
      { cause },
    );
  }
}

/**
 * Returns a pair to "strangers": no friendship, no pending request in
 * either direction, no block in either direction.
 *
 * Uses the same public endpoints a player would and touches nothing else.
 * Every call tolerates "already absent", because the point is the end state
 * and not the transition.
 *
 * The tokens are the ones the global setup captured. They are **not**
 * refreshed here, and that is the point: a refresh rotates the cookie, so a
 * spec that refreshed would invalidate the very session its browser context
 * is about to load. One holder per session, and the setup is it.
 *
 * Access tokens last fifteen minutes and a run takes seconds, so a token
 * minted at setup is still valid throughout.
 */
export async function resetRelationship(
  request: APIRequestContext,
  a: SeededAccount,
  b: SeededAccount,
): Promise<void> {
  for (const [other, token] of [
    [b, a.accessToken],
    [a, b.accessToken],
  ] as const) {
    const headers = { Authorization: `Bearer ${token}` };

    await request.delete(`${API}/api/v1/blocks/${other.id}`, {
      headers,
      failOnStatusCode: false,
    });
    await request.delete(`${API}/api/v1/friends/${other.id}`, {
      headers,
      failOnStatusCode: false,
    });

    for (const direction of ["outgoing", "incoming"] as const) {
      const listed = await request.get(`${API}/api/v1/friends/requests/${direction}`, {
        headers,
        failOnStatusCode: false,
      });
      if (!listed.ok()) continue;
      const page = (await listed.json()) as {
        data: { items: { id: string; player: { id: string } }[] };
      };
      for (const row of page.data.items.filter((item) => item.player.id === other.id)) {
        await request.delete(`${API}/api/v1/friends/requests/${row.id}`, {
          headers,
          failOnStatusCode: false,
        });
        await request.post(`${API}/api/v1/friends/requests/${row.id}/decline`, {
          headers,
          failOnStatusCode: false,
        });
      }
    }
  }
}

/**
 * Returns one account to "no push" — A64-021.6.
 *
 * The same shape as `resetRelationship` and for the same reason: **the
 * accounts accumulate state.** A run that enabled push and failed before its
 * last step leaves the subscription registered and the preference on, so the
 * next run opens the settings screen in `active` and never finds the button
 * it came to press.

 * Through the same public endpoints a player uses — never a truncation — and
 * every call tolerates "already absent", because the point is the end state
 * and not the transition.
 *
 * The endpoint is the one the spec's stubbed `PushManager` issues, which is
 * the only one this account can have: the browser it runs in is the only
 * thing that registers for it.
 */
export async function resetPush(
  request: APIRequestContext,
  account: SeededAccount,
  endpoint: string,
): Promise<void> {
  const headers = { Authorization: `Bearer ${account.accessToken}` };

  await request.post(`${API}/api/v1/notifications/push/subscriptions/remove`, {
    headers,
    data: { endpoint },
    failOnStatusCode: false,
  });
  await request.patch(`${API}/api/v1/notifications/preferences`, {
    headers,
    data: {
      changes: [{ category: "tournament", channel: "push", enabled: false }],
    },
    failOnStatusCode: false,
  });
}
