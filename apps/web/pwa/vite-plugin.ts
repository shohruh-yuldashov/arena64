import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { build, type Plugin } from "vite";

/**
 * Builds Arena64's service worker — A64-020.9 §4, §8, §9.
 *
 * ## Why this instead of `vite-plugin-pwa`
 *
 * `vite-plugin-pwa@1.3.0` does support Vite 8, and it would have been the
 * boring choice (CLAUDE.md §1.4) had the requirement been the one Workbox
 * is built for: a routing table with several strategies, expiration
 * plugins, and broad runtime caching. Arena64's policy is the opposite of
 * that — precache the shell, cache-first the hashed chunks, and *do not
 * handle anything else at all* (§10). Expressed in Workbox that is
 * `injectManifest` plus the same handlers written by hand, on top of
 * `workbox-build` and `workbox-window`.
 *
 * So the dependency would buy a manifest generator. That is this file:
 * a hundred lines, no transitive tree, and every caching decision readable
 * in `cache-policy.ts` rather than inside a generated artefact. §4's own
 * test — *"only if it materially reduces unsafe custom service-worker
 * code"* — is not met here, and §4's alternative — *"if a custom service
 * worker is simpler and safer for the actual requirements, use it"* — is.
 * Recorded in `docs/07-decisions/ADR-003-pwa-service-worker.md`.
 *
 * ## What it does
 *
 *   1. computes the precache manifest from the **actual** application
 *      bundle — the shell document, the entry chunk and its static
 *      imports, the CSS, and the fixed public assets. Never the lazy route
 *      chunks (§30)
 *   2. derives a cache version from the content of everything precached,
 *      so a new build gets a new cache and the old one is dropped (§25)
 *   3. builds `pwa/service-worker.ts` in a **second, isolated pass**, with
 *      those two values compiled in, emitted at `/sw.js` so the worker's
 *      scope is the site root and no wider (§31)
 *   4. fails the build if the emitted worker is not a self-contained
 *      classic script
 *
 * ## Why a second build rather than a second Rollup entry
 *
 * A second entry in the same build shares chunks with the first. The
 * moment the application imported one constant from `cache-policy` — which
 * it does, so that the worker's message contract has a single definition
 * (CLAUDE.md §3.4) — that module was split out and `sw.js` became an ES
 * module with an `import` statement in it. A service worker registered as
 * a classic script cannot run one, so it would have failed to register in
 * every browser at once. The guard below caught it; the fix is structural.
 * The worker gets its own build, so there is no bundle for it to share.
 *
 * `apply: "build"` — there is **no service worker in `npm run dev`** (§8).
 * A stale worker serving yesterday's shell over Vite's HMR is a debugging
 * session nobody should have to have; the production preview is how the
 * PWA is exercised locally, documented in `specs/frontend.md` §20.
 */

const SW_FILE_NAME = "sw.js";
const SW_SOURCE = "pwa/service-worker.ts";

/** The shell document, as a client requests it. */
const SHELL_URL = "/";

/**
 * Public assets precached by name, because Vite copies `public/` outside
 * the bundle and nothing in it is content-hashed.
 *
 * Listed rather than globbed: a glob would silently start precaching
 * whatever somebody drops into `public/` next, and an unbounded precache
 * is exactly what §9 forbids. Their bytes feed the version hash below, so
 * editing `offline.html` still invalidates the cache.
 */
const PUBLIC_PRECACHE = [
  "/offline.html",
  "/manifest.webmanifest",
  "/icons/favicon.svg",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon-maskable-512.png",
  "/icons/apple-touch-icon.png",
] as const;

// --- the pure half, exported so it can be tested without running a build ----

/** The parts of a Rollup output chunk this plugin reads. */
export interface BundleChunk {
  type: "chunk";
  name: string;
  fileName: string;
  isEntry: boolean;
  /** File names of chunks this one imports **statically**. */
  imports: string[];
  viteMetadata?: { importedCss?: Set<string> } | undefined;
}

export interface BundleAsset {
  type: "asset";
  fileName: string;
}

export type BundleItem = BundleChunk | BundleAsset;

/**
 * Everything needed to start the application, and nothing else.
 *
 * The walk follows **static** imports only. A lazy route chunk is reached
 * through `dynamicImports`, which this deliberately does not read: §30 says
 * not to precache twenty-three routes because they exist, and the runtime
 * asset cache picks each one up the first time a player opens it.
 */
export function shellAssetsFrom(bundle: Record<string, BundleItem>): string[] {
  const entry = Object.values(bundle).find(
    (item): item is BundleChunk => item.type === "chunk" && item.isEntry,
  );
  if (entry === undefined) {
    throw new Error("PWA: no application entry chunk in the bundle — cannot precache a shell.");
  }

  const collected = new Set<string>();
  const queue = [entry.fileName];

  while (queue.length > 0) {
    const fileName = queue.pop();
    if (fileName === undefined || collected.has(fileName)) continue;
    collected.add(fileName);

    const chunk = bundle[fileName];
    if (chunk === undefined || chunk.type !== "chunk") continue;
    queue.push(...chunk.imports);
    for (const css of chunk.viteMetadata?.importedCss ?? []) collected.add(css);
  }

  return [...collected].map((fileName) => `/${fileName}`).sort();
}

/**
 * The full precache list: the shell document, its build assets, the fixed
 * public files. Sorted and de-duplicated so the version hash below depends
 * on content rather than on bundle iteration order.
 */
export function precacheUrls(shellAssets: readonly string[]): string[] {
  return [...new Set([SHELL_URL, ...shellAssets, ...PUBLIC_PRECACHE])].sort();
}

/**
 * The cache version — a fingerprint of everything precached.
 *
 * Not the package version and not a timestamp. A timestamp would mint a
 * fresh cache on every build including one that changed nothing, and
 * `package.json`'s version would leave `offline.html` edits invisible to
 * the cache. Hashing the manifest *and the bytes of the unhashed public
 * files* is the only value that changes exactly when the cached content
 * does — which is what makes `activate`'s cleanup correct rather than
 * approximately correct.
 */
export function cacheVersion(urls: readonly string[], publicBytes: readonly Buffer[]): string {
  const hash = createHash("sha256");
  hash.update(urls.join("\n"));
  for (const bytes of publicBytes) hash.update(bytes);
  return hash.digest("hex").slice(0, 12);
}

/**
 * What a shippable worker looks like, asserted rather than assumed.
 *
 * Both failures are silent in production and expensive to diagnose: a
 * worker with module syntax never registers, and a worker still carrying a
 * placeholder throws on install. Both are cheap to detect here, so both
 * fail the build.
 */
export function assertShippableWorker(code: string): void {
  if (/^\s*(?:import|export)\b/m.test(code)) {
    throw new Error(
      "PWA: the compiled service worker contains module syntax — it would fail to register as a classic script.",
    );
  }
  for (const placeholder of ["__ARENA64_PRECACHE__", "__ARENA64_VERSION__"]) {
    if (code.includes(placeholder)) {
      throw new Error(
        `PWA: ${placeholder} survived into the compiled service worker — the build did not inject its precache manifest.`,
      );
    }
  }
}

// --- the plugin ------------------------------------------------------------

export function arena64Pwa(): Plugin {
  let root = "";
  let outDir = "";
  let publicDir = "";
  let precache: { urls: string[]; version: string } | null = null;

  return {
    name: "arena64:pwa",
    apply: "build",
    enforce: "post",

    configResolved(resolved) {
      root = resolved.root;
      outDir = resolve(resolved.root, resolved.build.outDir);
      publicDir = resolved.publicDir;
    },

    generateBundle(_options, bundle) {
      const urls = precacheUrls(shellAssetsFrom(bundle));
      const publicBytes = PUBLIC_PRECACHE.map((url) =>
        readFileSync(join(publicDir, url.slice(1))),
      );
      precache = { urls, version: cacheVersion(urls, publicBytes) };
    },

    /**
     * After the application has been written, because the worker's content
     * depends on what was written and because the second build writes into
     * the same directory.
     */
    async closeBundle() {
      if (precache === null) {
        throw new Error(
          "PWA: the precache manifest was never computed — no bundle was generated.",
        );
      }

      await build({
        // `configFile: false` — this build must not load `vite.config.ts`,
        // which would load this plugin, which would start this build.
        configFile: false,
        root,
        logLevel: "warn",
        define: {
          __ARENA64_PRECACHE__: JSON.stringify(precache.urls),
          __ARENA64_VERSION__: JSON.stringify(precache.version),
        },
        build: {
          outDir,
          // The application is already there. Emptying would delete it.
          emptyOutDir: false,
          // No map: nothing loads `sw.js` in a debugger by URL, and a map
          // beside it is one more file to decide about precaching.
          sourcemap: false,
          rollupOptions: {
            // One input, and nothing in the worker's graph is dynamically
            // imported, so this build has nothing to split — the whole
            // worker is one file. That is a property of the input rather
            // than a setting, which is why `assertShippableWorker` checks
            // it afterwards instead of a flag promising it beforehand.
            input: resolve(root, SW_SOURCE),
            output: { entryFileNames: SW_FILE_NAME },
          },
        },
      });

      assertShippableWorker(readFileSync(join(outDir, SW_FILE_NAME), "utf8"));
    },
  };
}
