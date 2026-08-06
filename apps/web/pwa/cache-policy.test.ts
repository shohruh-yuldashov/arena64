import { expect, it } from "vitest";

import { classify, type Handling } from "./cache-policy";

/**
 * What the service worker is allowed to touch — A64-020.9 §28.3, §31.
 *
 * The most expensive mistake a service worker can make is caching one
 * authenticated response: the cache is shared by every session on the
 * device, so the next person to sign in on that phone reads the last
 * person's data. This asserts, against the real classifier, that no such
 * response can reach a cache — and that the ones that can are exactly the
 * public, immutable build assets.
 */

const ORIGIN = "https://arena64.gg";

const context = {
  origin: ORIGIN,
  precached: new Set(["/", "/offline.html", "/assets/index-AAA.js"]),
};

function handling(url: string, options: { method?: string; mode?: string } = {}): Handling {
  return classify(
    { url, method: options.method ?? "GET", mode: options.mode ?? "cors" },
    context,
  );
}

it("never lets an authenticated, realtime, or third-party request reach a cache", () => {
  const untouchable = [
    // Authentication, in every form it takes — §12.
    `${ORIGIN}/api/v1/auth/browser/refresh`,
    `${ORIGIN}/api/v1/auth/browser/login`,
    `${ORIGIN}/api/v1/auth/browser/logout`,
    // The one-time socket credential. Caching it would replay a spent
    // ticket and break the connection it was minted for — §13.
    `${ORIGIN}/api/v1/auth/ws-ticket`,
    // Private reads. Every one of them is "whoever is holding the token".
    `${ORIGIN}/api/v1/profile/me`,
    `${ORIGIN}/api/v1/matchmaking/tickets/me`,
    `${ORIGIN}/api/v1/matches/019fe3/replay`,
    `${ORIGIN}/api/v1/games/history`,
    `${ORIGIN}/api/v1/tournaments/019fe1/registration`,
    // The gateway is an upgrade handshake, not a resource — §13.
    `${ORIGIN}/ws?ticket=abc`,
    // Another origin's caching is that origin's decision.
    "https://cdn.example.com/assets/index-AAA.js",
    `https://images.example.com/avatars/019fb9.png`,
  ];

  for (const url of untouchable) {
    expect(handling(url), url).toBe("network");
  }

  // A mutation is not a lookup, whatever it is addressed to.
  expect(handling(`${ORIGIN}/api/v1/tournaments/019fe1/entries`, { method: "POST" })).toBe(
    "network",
  );
  expect(handling(`${ORIGIN}/assets/index-AAA.js`, { method: "POST" })).toBe("network");

  // **Ordered on purpose.** Opening `/api/v1/docs` in a tab is a
  // *navigation* to the API, and answering it with the application shell
  // would replace a real response with a lie.
  expect(handling(`${ORIGIN}/api/v1/docs`, { mode: "navigate" })).toBe("network");
});

it("serves the shell for navigations and caches hashed build assets on demand", () => {
  // Every in-app route is the one cached document — the router owns the
  // path, so no route is ever stored as its own HTML snapshot (§9).
  expect(handling(`${ORIGIN}/`, { mode: "navigate" })).toBe("navigate");
  expect(handling(`${ORIGIN}/games/019fe3`, { mode: "navigate" })).toBe("navigate");
  expect(handling(`${ORIGIN}/tournaments/019fe1`, { mode: "navigate" })).toBe("navigate");

  // Installed with the shell.
  expect(handling(`${ORIGIN}/assets/index-AAA.js`)).toBe("precache");
  expect(handling(`${ORIGIN}/offline.html`)).toBe("precache");

  // A lazy route chunk: cached the first time it is asked for, because its
  // name contains its own hash and can never mean different bytes (§30).
  expect(handling(`${ORIGIN}/assets/tournament-DDD.js`)).toBe("asset");

  // Only a developer's devtools ever wants a map.
  expect(handling(`${ORIGIN}/assets/index-AAA.js.map`)).toBe("network");
  // The worker itself is the browser's to update, not the worker's to cache.
  expect(handling(`${ORIGIN}/sw.js`)).toBe("network");
});
