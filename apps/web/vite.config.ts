import { fileURLToPath } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

import { arena64Pwa } from "./pwa/vite-plugin.ts";

/**
 * One config for the app and its unit tests — `vitest/config` re-exports
 * Vite's own `defineConfig`, so a second file would only be a second place
 * for the `@` alias to drift out of step with `tsconfig.json`.
 *
 * Playwright is deliberately **not** here: it drives a real browser
 * against a built app and shares nothing with this pipeline except the
 * dev-server command, which `playwright.config.ts` starts itself.
 */
/**
 * Where the API lives during development.
 *
 * The browser must see **one origin** — the refresh cookie is `HttpOnly`
 * and same-site, so a cross-origin call would either not carry it or would
 * need `SameSite=None`, which is the CSRF exposure the cookie exists to
 * avoid. So the dev server proxies `/api` to FastAPI and the page only
 * ever talks to itself.
 *
 * Production has the same contract, enforced by a reverse proxy rather
 * than by this file — see `specs/frontend.md` §12. Nothing in the app
 * hardcodes a host: the client's base URL is the relative `/api/v1`.
 */
const API_TARGET = process.env.ARENA64_API_TARGET ?? "http://localhost:8000";

const API_PROXY = {
  "/api": {
    target: API_TARGET,
    // `changeOrigin: false` on purpose: the backend's CSRF check reads
    // `Origin`, and rewriting it would make every request claim to come
    // from the API's own host — exercising a check production applies
    // differently.
    changeOrigin: false,
    ws: false,
  },
  /**
   * The gateway — A64-020.5A §20.
   *
   * `/ws` is mounted at the **application root**, not under `/api/v1`
   * (`app/app_factory.py`), so the `/api` rule above does not reach it and
   * a second entry is required rather than optional. Configured now, ahead
   * of the client that uses it, because the alternative is discovering it
   * in the phase that also has a socket to debug.
   *
   * `ws: true` is the whole point: without it Vite proxies the HTTP
   * request and drops the `Upgrade` handshake, which presents as a socket
   * that connects and immediately closes with no error anybody can act on.
   *
   * **Same origin, like `/api`.** The page talks only to itself, so the
   * socket carries the same cookies and the same `Origin` the API already
   * checks. Production must route `/ws` with WebSocket upgrade through its
   * reverse proxy — the contract this file expresses for development and
   * `specs/frontend.md` §12 states for deployment.
   *
   * Authentication is **not** a proxy concern: the gateway takes a one-time
   * ticket from `POST /auth/ws-ticket` as a query parameter, which
   * A64-020.5B wires. Nothing here needs to know that, and nothing here
   * forwards a token.
   */
  "/ws": {
    target: API_TARGET,
    changeOrigin: false,
    ws: true,
  },
};

export default defineConfig({
  // `arena64Pwa` is `apply: "build"` — it adds the service-worker entry and
  // its precache manifest to the production bundle and does nothing at all
  // in `npm run dev`, which is A64-020.9 §8's rule that a stale worker must
  // never sit between a developer and Vite's HMR.
  plugins: [react(), tailwindcss(), arena64Pwa()],
  // `server` is `npm run dev`; `preview` is the built app, which the e2e
  // suite drives. Both need the proxy, and `preview` does **not** inherit
  // it — a mismatch there presents as an e2e suite that cannot sign in
  // while development works fine.
  server: {
    proxy: API_PROXY,
    /**
     * `app.localhost` — A64-024.2H §8.
     *
     * A tunnel host (ngrok, Cloudflare) goes here while one is in use and
     * comes back out with it. The names are ephemeral, so a committed one
     * is a name that will never be requested again.
     *
     * **Ports do not separate cookies.** `localhost:5173` and
     * `localhost:5174` are one host to the cookie jar, so developing the
     * player client and the admin console on bare `localhost` makes them
     * share a session — which proves nothing about production and hides
     * the isolation the console depends on.
     *
     * Production keeps them apart by host (`arena64.gg` and
     * `admin.arena64.gg`), and `*.localhost` reproduces that locally with
     * no `/etc/hosts` entry.
     */
    allowedHosts: ["app.localhost", "localhost"],
  },
  preview: { proxy: API_PROXY },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    // Every route is a dynamic import (`app/router/routes.tsx`), so Rollup
    // already emits one chunk per page. No `manualChunks` here on purpose:
    // hand-partitioning a bundle before there is a bundle to measure is
    // exactly the premature optimisation CLAUDE.md §10.1 forbids.
    sourcemap: true,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/shared/test/setup.ts"],
    // `pwa/` is not under `src/` — it builds a script with its own global
    // scope (A64-020.9 §8) — but its cache policy is the security-relevant
    // half of the service worker and is tested like any other module.
    include: ["src/**/*.test.{ts,tsx}", "pwa/**/*.test.ts"],
    // Playwright's specs live in `tests/e2e` and are driven by Playwright.
    // Without this, Vitest would collect them and fail on `test.describe`.
    exclude: ["node_modules/**", "tests/e2e/**"],
    css: false,
    restoreMocks: true,
  },
});
