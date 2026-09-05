import { afterEach, expect, it, vi } from "vitest";

import { accessToken } from "@/app/session-store";
import { refresh } from "@/shared/api/client";

/**
 * One refresh exchange per tab — A64-027A.5 §19.
 *
 * The bug this guards was a lost session on page load, and it was invisible
 * for three tasks because nothing here is wrong in isolation: each refresh
 * is correct, and the server destroying a session on token reuse is the
 * defence working. Only the *pair* is the defect, so only a test that fires
 * two at once can see it.
 */

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

afterEach(() => {
  vi.unstubAllGlobals();
  accessToken.clear();
});

/** A fetch that answers refreshes only when `release()` is called. */
function deferredRefresh() {
  let release: (() => void) | undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const calls: string[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
    calls.push(String(input));
    await gate;
    return json({ data: { access_token: "fresh" } });
  });
  return { calls, release: release as () => void };
}

it("spends the rotating token once when two callers refresh at the same time", async () => {
  // The refresh token rotates on use: two exchanges started before the
  // first `Set-Cookie` lands present the same value, the second is reuse,
  // and the server correctly destroys the session. `StrictMode` fires
  // exactly this pair on every load.
  const { calls, release } = deferredRefresh();

  const both = Promise.all([refresh(), refresh()]);
  release();
  const [first, second] = await both;

  expect(calls).toHaveLength(1);
  expect(first).toEqual({ status: "ok", value: "fresh" });
  expect(second).toEqual(first);
});

it("exchanges again once the first has settled", async () => {
  // Single-flight must not become single-ever: a later navigation needs a
  // real exchange, not the answer to the one before it.
  const { release } = deferredRefresh();
  release();
  await refresh();

  const { calls, release: releaseAgain } = deferredRefresh();
  const next = refresh();
  releaseAgain();
  await next;

  expect(calls).toHaveLength(1);
});

it("abandons an exchange belonging to a session that ended", async () => {
  // Sign-out while a refresh is in flight: its result belongs to the old
  // session, and the next sign-in must not be handed it.
  const { calls, release } = deferredRefresh();
  const abandoned = refresh();

  accessToken.clear();

  const { calls: after, release: releaseAfter } = deferredRefresh();
  const fresh = refresh();
  release();
  releaseAfter();
  await Promise.all([abandoned, fresh]);

  expect(calls).toHaveLength(1);
  expect(after).toHaveLength(1);
});
