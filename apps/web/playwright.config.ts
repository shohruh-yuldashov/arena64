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
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
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
