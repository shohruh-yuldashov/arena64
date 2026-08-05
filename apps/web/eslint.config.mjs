import js from "@eslint/js";
import prettier from "eslint-config-prettier";
import importPlugin from "eslint-plugin-import";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import simpleImportSort from "eslint-plugin-simple-import-sort";
import unusedImports from "eslint-plugin-unused-imports";
import globals from "globals";
import tseslint from "typescript-eslint";

/**
 * ## The layer rule is a lint rule, not a convention
 *
 * `apps/api` has `import-linter` and 27 contracts that fail a build when a
 * module reaches somewhere it should not. This is the frontend's
 * equivalent, and it exists for the same reason: a dependency direction
 * that is only written down is a dependency direction that drifts.
 *
 *     shared  <-  entities  <-  features  <-  widgets  <-  pages  <-  app
 *
 * Each layer may import **strictly lower** layers and nothing else. So a
 * `shared/ui` primitive cannot reach a page, a widget cannot reach the
 * provider graph, and nothing below `app` can import the router — which is
 * what keeps `shared` genuinely reusable rather than quietly coupled to
 * whatever imported it first.
 *
 * Expressed as `import/no-restricted-paths` zones below: for every layer,
 * every layer above it is forbidden as a *source*.
 */
const LAYERS = ["shared", "entities", "features", "widgets", "pages", "app"];

const layerZones = LAYERS.flatMap((target, index) =>
  LAYERS.slice(index + 1).map((from) => ({
    target: `./src/${target}`,
    from: `./src/${from}`,
    message: `${target} may not import ${from} — dependencies point downward (specs/frontend.md §3).`,
  })),
);

export default tseslint.config(
  {
    ignores: [
      "dist",
      "node_modules",
      "src/shared/api/generated",
      "playwright-report",
      "test-results",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2023,
      globals: { ...globals.browser, ...globals.node },
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    settings: {
      // Without a resolver the `@/` alias is an unresolvable string, and
      // `import/no-restricted-paths` silently matches nothing — a lint rule
      // that reports clean because it cannot see any imports at all. Caught
      // by deliberately writing a violation and finding that lint passed.
      "import/resolver": {
        typescript: { project: "./tsconfig.json" },
      },
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
      import: importPlugin,
      "simple-import-sort": simpleImportSort,
      "unused-imports": unusedImports,
    },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],

      // Import hygiene — the two the task names explicitly.
      "simple-import-sort/imports": "error",
      "simple-import-sort/exports": "error",
      "unused-imports/no-unused-imports": "error",
      "@typescript-eslint/no-unused-vars": "off",
      "unused-imports/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],

      // The architecture rule. See this file's header.
      "import/no-restricted-paths": ["error", { zones: layerZones }],

      // A promise nobody awaits is CLAUDE.md §2.7's "swallowed rejection"
      // in its most common frontend disguise.
      "@typescript-eslint/no-floating-promises": "error",
    },
  },
  {
    // `react-refresh/only-export-components` guards hot-module-reload
    // ergonomics: a file exporting both a component and something else
    // makes HMR fall back to a full reload. Every shadcn/ui primitive
    // trips it by construction — `buttonVariants` beside `Button`, Radix
    // parts re-exported beside wrappers — and so does a context module,
    // which exists precisely to export a provider and its hook together.
    // Splitting them to satisfy a dev-server optimisation would make the
    // source worse to read for no runtime benefit.
    files: ["src/shared/ui/**", "src/shared/theme/**"],
    rules: { "react-refresh/only-export-components": "off" },
  },
  {
    // Tests reach across layers on purpose — a reachability test's whole
    // job is to assert what the app wires together, which means importing
    // from `app` while living beside the module it covers.
    files: ["**/*.test.{ts,tsx}", "src/shared/test/**", "tests/**"],
    rules: {
      "import/no-restricted-paths": "off",
      "@typescript-eslint/no-unsafe-assignment": "off",
      "@typescript-eslint/no-unsafe-member-access": "off",
      "@typescript-eslint/no-unsafe-call": "off",
    },
  },
  {
    files: ["**/*.config.{ts,mjs}"],
    extends: [tseslint.configs.disableTypeChecked],
  },
  prettier,
);
