import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end, against a real browser and a real build of this app.
 *
 * Deliberately thin at the foundation stage: one smoke journey that proves
 * the shell boots, routes, and is keyboard-navigable. Journeys belong to
 * the phases that build them — an e2e suite written before there is
 * anything to journey through is a suite that tests its own fixtures.
 *
 * `npm run preview` rather than `npm run dev`: the thing worth asserting
 * is the artefact that ships, including the code-split chunks the dev
 * server never produces.
 */
export default defineConfig({
  testDir: "./tests/e2e",
  // Seeds the E2E accounts once, before any worker starts — A64-020.4 §21.
  // Authentication is rate-limited, so it happens here and the resulting
  // browser sessions are reused; see `tests/e2e/global-setup.ts`.
  globalSetup: "./tests/e2e/global-setup.ts",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: "http://localhost:4173",
    // The preview server proxies nothing, so the browser must reach the API
    // at the same origin it reaches the page. `vite preview` honours the
    // dev-server proxy config, which is what makes `/api/v1` same-origin
    // here exactly as it is in `npm run dev`.
    trace: "on-first-retry",
  },
  // **Seven projects, because four specs share three accounts.**

  //
  // `play.spec.ts`, `game.spec.ts`, `game-controls.spec.ts` and
  // `realtime-push.spec.ts` all drive the lobby with
  // `e2e_lobby_one|two|three`, and running them at once does not merely
  // race: refresh tokens rotate, so two contexts refreshing one session
  // means the loser presents a superseded token and the server revokes the
  // **whole chain** — by design (A64-020.2). The next run then has no
  // session, falls through to the login path, and five logins per IP per
  // fifteen minutes runs out three specs later. That is what a shared
  // account looks like from the outside: a suite that fails on
  // authentication for reasons no spec mentions.
  //
  // Project dependencies are the only cross-file ordering Playwright has —
  // `fullyParallel: false` still spreads *files* across workers, and
  // `mode: "serial"` does not reach past one file. `live-game` starts only
  // once `lobby` has finished, so the accounts are used by one spec at a
  // time while everything else still runs in parallel.
  //
  // Separate accounts were the other option and cost more than they save:
  // three registrations is the entire hourly cap for this IP, which would
  // make the first run after seeding fail on `auth.spec.ts`.
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
      testIgnore: [
        "**/play.spec.ts",
        "**/game.spec.ts",
        "**/game-controls.spec.ts",
        "**/realtime-push.spec.ts",
        "**/replay.spec.ts",
        "**/history.spec.ts",
        "**/tournament.spec.ts",
      ],
    },
    {
      name: "lobby",
      use: { ...devices["Desktop Chrome"] },
      testMatch: "**/play.spec.ts",
    },
    {
      name: "live-game",
      use: { ...devices["Desktop Chrome"] },
      testMatch: "**/game.spec.ts",
      dependencies: ["lobby"],
    },
    {
      name: "history",
      use: { ...devices["Desktop Chrome"] },
      testMatch: "**/history.spec.ts",
      // After `replay`, for the same reason `replay` is after the game
      // projects: it reads matches those projects finished. It queues for
      // nothing, so it contends with nobody — A64-020.5F §28.
      dependencies: ["replay"],
    },
    {
      name: "tournament",
      use: { ...devices["Desktop Chrome"] },
      testMatch: "**/tournament.spec.ts",
      // **Last in the chain** — A64-020.6 §28. It waits on nothing any
      // earlier project *produces*: it creates its own tournament through
      // the operator command and never queues. What it waits for is the
      // account — it drives `e2e_lobby_three`, and two specs refreshing one
      // session revoke it, which is the note above.
      //
      // Borrowing `e2e_profile_owner` instead was tried, ran beside
      // `profile.spec.ts`, and failed in exactly that way. A seventh seeded
      // account was the other option and costs a registration, which is
      // three per IP per hour and already spent by `auth.spec.ts`.
      dependencies: ["history"],
    },
    {
      name: "replay",
      use: { ...devices["Desktop Chrome"] },
      testMatch: "**/replay.spec.ts",
      // **Last, and for a different reason than the others.** It does not
      // queue, so it does not contend for the accounts — it needs a match
      // one of the earlier projects *finished*, which
      // `game-controls.spec.ts` produces by resignation. A64-020.5E §26.
      dependencies: ["realtime-push"],
    },
    {
      name: "realtime-push",
      use: { ...devices["Desktop Chrome"] },
      testMatch: "**/realtime-push.spec.ts",
      // Fourth in the chain. Same three accounts, same reason.
      dependencies: ["game-controls"],
    },
    {
      name: "game-controls",
      use: { ...devices["Desktop Chrome"] },
      testMatch: "**/game-controls.spec.ts",
      // Third in the chain for the same reason `live-game` is second: it
      // drives the lobby with the same three accounts, and two specs
      // refreshing one session revoke it — see the note above.
      dependencies: ["live-game"],
    },
  ],
  webServer: {
    command: "npm run build && npm run preview -- --port 4173 --strictPort",
    // **Never reuse** — A64-020.5D §18.
    //
    // `reuseExistingServer: !process.env.CI` cost this project a debugging
    // session: a `vite preview` left running from an earlier invocation
    // kept serving a bundle built two hours before, so several runs
    // exercised old code while reporting on new specs. Nothing failed
    // loudly; the tests simply asserted against a frontend that no longer
    // existed.
    //
    // Option A of the two §18 offers, chosen because it is the one with no
    // moving parts: a build-hash handshake needs the server to publish a
    // hash, the config to read it, and both to agree on where — three
    // places to get wrong in order to detect a problem that not reusing
    // simply cannot have.
    //
    // The cost is one rebuild per run, which is ~250ms here. `strictPort`
    // means an occupied 4173 fails with a clear bind error rather than
    // silently attaching to whatever is there — and it fails **without**
    // killing anything, because a process this config did not start is not
    // this config's to kill (§18).
    reuseExistingServer: false,
    // `localhost`, not `127.0.0.1`: Vite's preview server binds the
    // hostname it was given, and on a machine where `localhost` resolves
    // to `::1` first the loopback IPv4 address never answers — which
    // presents as a 120-second webServer timeout with no other symptom.
    url: "http://localhost:4173",
    timeout: 120_000,
  },
});
