import { fileURLToPath } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

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
};

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // `server` is `npm run dev`; `preview` is the built app, which the e2e
  // suite drives. Both need the proxy, and `preview` does **not** inherit
  // it — a mismatch there presents as an e2e suite that cannot sign in
  // while development works fine.
  server: { proxy: API_PROXY },
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
    include: ["src/**/*.test.{ts,tsx}"],
    // Playwright's specs live in `tests/e2e` and are driven by Playwright.
    // Without this, Vitest would collect them and fail on `test.describe`.
    exclude: ["node_modules/**", "tests/e2e/**"],
    css: false,
    restoreMocks: true,
  },
});
