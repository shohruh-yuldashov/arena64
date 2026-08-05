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
export default defineConfig({
  plugins: [react(), tailwindcss()],
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
