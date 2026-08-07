import { execFile } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { promisify } from "node:util";

import { request } from "@playwright/test";

import {
  API,
  AUTH_DIR,
  E2E_ACCOUNTS,
  PASSWORD,
  type SeededAccount,
  statePath,
} from "./accounts";

const run = promisify(execFile);

/** The API directory, from `apps/web`. */
const API_DIR = resolve(process.cwd(), "..", "api");

/**
 * Seeds the E2E accounts **once**, before any worker starts — A64-020.4 §21.
 *
 * ## Why this exists rather than a fixture per spec
 *
 * Two rate limits, and between them they make per-spec authentication
 * impossible:
 *
 *     register   3 per IP per hour
 *     login      5 per IP per 15 minutes
 *
 * A64-020.3's suite registered one account per run and became unrunnable on
 * the fourth. A social suite needs two accounts, so it would have hit the
 * register cap on its second run and the login cap on its first — three
 * specs signing in through the form is already three of the five.
 *
 * So authentication happens here, serially, and the resulting **browser
 * session** is written to disk. Specs load it as `storageState` and never
 * sign in at all.
 *
 * ## What a repeat run actually costs
 *
 * The saved state carries the `HttpOnly` refresh cookie, which lasts thirty
 * days, and each run probes it before re-authenticating.
 *
 *     e2e_social_alice / _bob   0 — their sessions survive every run
 *     e2e_profile_owner         1 login — that spec asserts "sign out
 *                               everywhere", which revokes its own session
 *                               by design, so the next run must sign in
 *     auth.spec                 1 registration — registration is its subject
 *     verify-email.spec         1 registration — A64-021.5H. An account
 *                               that is *not* yet verified is the whole
 *                               subject, and every seeded account is, so
 *                               this one cannot be borrowed
 *
 * So a run costs one login and **two** registrations. With `register_ip` at
 * ten an hour (A64-020.6 raised it from three) that is roughly five full
 * runs an hour from one IP — and a sixth fails at registration rather than
 * at an assertion, which reads as a broken spec and is not one. Clear the
 * buckets rather than debugging it:
 *
 *     uv run python -m app.operator.rate_limits clear
 *
 * Stated in `specs/frontend.md` §10.1 rather than discovered.
 *
 * ## What was deliberately not done
 *
 * - **Turning the rate limit off.** It is production behaviour, and a suite
 *   that only passes without it never exercises it.
 * - **Clearing Redis from the test suite.** Reaching into the backend's
 *   infrastructure from a frontend spec is not a fixture, it is a hole.
 * - **Failing silently.** Every path here throws with the API's own status,
 *   so a broken fixture fails the run rather than skipping it.
 */
export default async function globalSetup(): Promise<void> {
  const context = await request.newContext({ baseURL: API });

  try {
    const reachable = await context
      .get("/health")
      .then((response) => response.ok())
      .catch(() => false);
    if (!reachable) {
      // Not an error: the specs themselves skip when the API is absent, and
      // this must not turn "no backend running" into a red suite.
      console.warn("[e2e] apps/api is not reachable — social specs will skip");
      return;
    }

    mkdirSync(AUTH_DIR, { recursive: true });
    const usernames = Object.values(E2E_ACCOUNTS);
    for (const username of usernames) {
      await seed(context, username);
    }
    await verifyAccounts(usernames.map((username) => `${username}@example.com`));
  } finally {
    await context.dispose();
  }
}

/**
 * Marks every fixture address verified — **A64-021.5H**.
 *
 * Every product route requires a verified address now, so an unverified
 * fixture account lands fourteen specs on `/verify-email` instead of the
 * page they came for.
 *
 * Through the repository's own operator command, the same class of entry
 * point `tournament.spec.ts` uses to create a tournament, rather than an
 * HTTP call. There is no endpoint that can do this and there must not be:
 * one would remove email verification from the platform for anything that
 * could reach the API.
 *
 * Called **once for all accounts, after the seed loop**, and both halves
 * are deliberate. One process rather than fourteen because each costs an
 * interpreter start. After the loop rather than inside it because `seed`
 * returns early for an account whose saved session still works — the
 * common case on a rerun — so a call inside would leave every long-lived
 * fixture account unverified forever, which is exactly how this was first
 * written and exactly how it failed. It is idempotent, so paying for it on
 * every run costs one process and changes nothing.
 *
 * `python -m app.operator.accounts verify`, against the same database the
 * API uses. Nothing here truncates a table, flushes Redis, disables a rate
 * limit, or reaches a browser-visible endpoint.
 */
async function verifyAccounts(emails: string[]): Promise<void> {
  const { stdout } = await run(
    "uv",
    [
      "run",
      "python",
      "-m",
      "app.operator.accounts",
      "verify",
      ...emails.flatMap((email) => ["--email", email]),
    ],
    { cwd: API_DIR },
  );
  const confirmed = stdout.match(/verified/g)?.length ?? 0;
  if (confirmed < emails.length) {
    throw new Error(
      `[e2e] verified ${confirmed} of ${emails.length} accounts: ${stdout.trim()}`,
    );
  }
}

async function seed(
  context: Awaited<ReturnType<typeof request.newContext>>,
  username: string,
): Promise<void> {
  const path = statePath(username);

  // A saved session that still works costs nothing to reuse. This is the
  // whole reason repeat runs do not touch either rate limit.
  if (existsSync(path) && (await sessionIsLive(path))) {
    return;
  }

  const email = `${username}@example.com`;
  const session = await context.post("/api/v1/auth/browser/login", {
    data: { email, password: PASSWORD },
    failOnStatusCode: false,
  });

  if (session.status() === 401) {
    const created = await context.post("/api/v1/auth/browser/register", {
      data: { username, email, password: PASSWORD },
      failOnStatusCode: false,
    });
    if (!created.ok()) {
      throw new Error(
        `[e2e] could not seed ${username}: register returned ${created.status()} — ` +
          `${await created.text()}`,
      );
    }
  } else if (!session.ok()) {
    throw new Error(`[e2e] could not sign in as ${username}: ${session.status()}`);
  }

  // **`identify` first, then the state.** It refreshes, and refreshing
  // *rotates* the cookie — capturing the jar before it would save a token
  // the server has already superseded, and presenting a superseded token
  // revokes the whole chain. That is precisely how the first version of
  // this file failed: every spec got a `401` from a session it had just
  // been handed.
  const account = await identify(context);
  const state = await context.storageState();
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify({ ...state, arena64: account }, null, 2));

  // Cleared so the next account starts from an empty jar rather than
  // inheriting this one's session.
  await context.storageState({ path: undefined });
}

/**
 * Whether a saved session still works, leaving the file holding the
 * **rotated** cookie.
 *
 * The rewrite is not optional: the probe consumes the stored token, so a
 * file left unchanged would be stale from the moment it was checked.
 */
async function sessionIsLive(path: string): Promise<boolean> {
  const probe = await request.newContext({ baseURL: API, storageState: path });
  try {
    const refreshed = await refreshOnce(probe);
    if (!refreshed.ok()) return false;

    // Rotated, so the stored cookie must be replaced or the *next* run
    // would present a superseded token and revoke the whole chain.
    //
    // The **access token is refreshed too**: the stored one is fifteen
    // minutes old at best and expired at worst, and `resetRelationship`
    // uses it directly. A live cookie beside a dead bearer token is the
    // subtle version of the same staleness.
    const saved = JSON.parse(readFileSync(path, "utf8")) as { arena64: SeededAccount };
    const body = (await refreshed.json()) as { data: { access_token: string } };
    const state = await probe.storageState();
    writeFileSync(
      path,
      JSON.stringify(
        { ...state, arena64: { ...saved.arena64, accessToken: body.data.access_token } },
        null,
        2,
      ),
    );
    return true;
  } finally {
    await probe.dispose();
  }
}

/**
 * One refresh, waiting out the rate limit rather than reading it as a
 * failure — A64-020.5B.
 *
 * Refresh is capped at 30 per IP per 60 seconds, and seeding spends one per
 * account, so a single run is comfortably inside it and two runs a minute
 * apart are not. A `429` is *"ask again in `Retry-After` seconds"* and
 * nothing else — the session behind the cookie is fine.
 *
 * Reading it as a dead session was the bug this replaces: `sessionIsLive`
 * returned `false`, seeding fell through to the login path and spent one of
 * five logins on a session that already worked, and `identify` then made an
 * unchecked refresh of its own that `429`d too — surfacing as
 * `Cannot read properties of undefined (reading 'user')` rather than as a
 * rate limit.
 *
 * One retry, bounded by the header's own answer. Not a loop: if the window
 * has passed and the second attempt still fails, the cause is not the limit
 * and pretending otherwise would hide it.
 */
async function refreshOnce(
  context: Awaited<ReturnType<typeof request.newContext>>,
): Promise<Awaited<ReturnType<typeof context.post>>> {
  const first = await context.post("/api/v1/auth/browser/refresh", {
    failOnStatusCode: false,
  });
  if (first.status() !== 429) return first;

  const retryAfter = Number(first.headers()["retry-after"] ?? "60");
  const waitMs = (Number.isFinite(retryAfter) ? Math.min(retryAfter, 60) : 60) * 1000 + 1_000;
  console.warn(`[e2e] refresh is rate limited — waiting ${Math.round(waitMs / 1000)}s`);
  await new Promise((resolve) => setTimeout(resolve, waitMs));

  return context.post("/api/v1/auth/browser/refresh", { failOnStatusCode: false });
}

async function identify(
  context: Awaited<ReturnType<typeof request.newContext>>,
): Promise<SeededAccount> {
  const refreshed = await refreshOnce(context);
  if (!refreshed.ok()) {
    throw new Error(
      `[e2e] could not identify a freshly signed-in session: refresh returned ` +
        `${refreshed.status()} — ${await refreshed.text()}`,
    );
  }

  const body = (await refreshed.json()) as {
    data: { access_token: string; user: { id: string; username: string } };
  };
  return {
    username: body.data.user.username,
    email: `${body.data.user.username}@example.com`,
    password: PASSWORD,
    id: body.data.user.id,
    accessToken: body.data.access_token,
  };
}
