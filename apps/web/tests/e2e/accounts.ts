import { readFileSync } from "node:fs";
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

/** Where the saved sessions live. Ignored by git — they carry a credential. */
export const AUTH_DIR = resolve(process.cwd(), "test-results/.auth");

export function statePath(username: string): string {
  return resolve(AUTH_DIR, `${username}.json`);
}

/** Stable, and namespaced so they cannot collide with a real player. */
export const E2E_ACCOUNTS = {
  alice: "e2e_social_alice",
  bob: "e2e_social_bob",
  /** The profile suite's own, so it stops registering per run too. */
  profile: "e2e_profile_owner",
} as const;

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
