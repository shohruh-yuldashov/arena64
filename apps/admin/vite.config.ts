import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/**
 * The admin console's build — AD-04.
 *
 * A **separate application**, not a route in `apps/web`. AD-04's reasoning
 * is the whole of why this file exists: moderation tooling can suspend
 * accounts and adjudicate matches, and shipping that code to every
 * player's browser puts the privileged surface one authorization bug away
 * from exposure while inflating the bundle that gates time-to-first-move.
 *
 * ## Session separation is the origin, not a flag
 *
 * `apps/web`'s refresh cookie is `HttpOnly`, `SameSite=Lax` and carries no
 * `Domain` attribute — so it is **host-only**. Serving this app from its
 * own origin therefore gives it its own cookie jar entry by construction:
 * an administrator signing in here does not touch the player session, and
 * a player session cannot be replayed here. Nothing enforces that in code
 * because nothing has to; it is a property of how cookies are scoped.
 *
 * The dev proxy mirrors production for the same reason `apps/web`'s does:
 * the browser must see one origin, or the cookie is either not sent or
 * needs `SameSite=None` — which is the CSRF exposure the cookie exists to
 * avoid.
 */
const API_TARGET = process.env.ARENA64_API_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    // Deliberately not `apps/web`'s port. Two apps on one port is one app,
    // and the separation this whole file is about would be a lie.
    port: 5174,
    /**
     * Reachable as `admin.localhost` as well as `localhost` — A64-024.2H §8.
     *
     * **Ports do not separate cookies.** `localhost:5173` and
     * `localhost:5174` are one host as far as the cookie jar is concerned,
     * so developing on them makes the two apps share a session — which
     * proves nothing about production and hides the very isolation this
     * console depends on.
     *
     * `*.localhost` resolves to the loopback address in every current
     * browser with no `/etc/hosts` entry, so `http://admin.localhost:5174`
     * beside `http://app.localhost:5173` reproduces production's host
     * separation locally. Nothing about the cookie is relaxed to achieve
     * it.
     */
    allowedHosts: ["admin.localhost", "localhost"],
    proxy: {
      // Same-origin with its own API path, exactly as production is
      // (`specs/frontend.md` §11). The console never calls a separate API
      // host for a cookie-authenticated request: the cookie belongs to
      // whichever host answered, and an API host shared with the player
      // client would be one cookie jar for both.
      "/api": { target: API_TARGET, changeOrigin: false, ws: false },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/shared/test/setup.ts"],
  },
});
