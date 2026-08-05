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
  // **Three projects, because two specs share three accounts.**
  //
  // `play.spec.ts` and `game.spec.ts` both drive the lobby with
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
      testIgnore: ["**/play.spec.ts", "**/game.spec.ts"],
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
  ],
  webServer: {
    command: "npm run build && npm run preview -- --port 4173 --strictPort",
    // `localhost`, not `127.0.0.1`: Vite's preview server binds the
    // hostname it was given, and on a machine where `localhost` resolves
    // to `::1` first the loopback IPv4 address never answers — which
    // presents as a 120-second webServer timeout with no other symptom.
    url: "http://localhost:4173",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
