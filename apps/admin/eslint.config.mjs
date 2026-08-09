import js from "@eslint/js";
import prettier from "eslint-config-prettier";
import globals from "globals";
import tseslint from "typescript-eslint";

/**
 * The admin console's lint rules — a deliberate subset of `apps/web`'s.
 *
 * Narrower on purpose: this app has no PWA, no service worker and no
 * import-boundary layers to police yet, so the plugins that enforce those
 * in `apps/web` would be configuration with nothing to check. What is kept
 * is what catches real defects — the TypeScript rules and unused code.
 */
export default tseslint.config(
  { ignores: ["dist", "node_modules", "coverage"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      globals: { ...globals.browser, ...globals.es2023 },
    },
  },
  prettier,
);
