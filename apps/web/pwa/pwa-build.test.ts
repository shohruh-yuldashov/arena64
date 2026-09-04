import { readFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { expect, it } from "vitest";

import {
  assertShippableWorker,
  type BundleItem,
  cacheVersion,
  precacheUrls,
  shellAssetsFrom,
} from "./vite-plugin";

/**
 * The two things about a PWA that are settled before the browser ever runs
 * it — A64-020.9 §28.1, §28.4, §32.
 *
 * Both are asserted against **the files that ship**: the manifest as it
 * sits in `public/`, the icons as they sit on disk, and the document that
 * links them. A test that built its own manifest object would prove that
 * the test could write JSON.
 */

/**
 * Both take their argument as a *variable*, deliberately: Vite rewrites a
 * literal `new URL("…", import.meta.url)` into an asset reference, and a
 * template literal into a glob that resolves to `undefined` when nothing
 * matches. Neither is what a test reading a source file wants.
 */
function pathTo(relative: string): string {
  return fileURLToPath(new URL(relative, import.meta.url));
}

function read(relative: string): string {
  return readFileSync(pathTo(relative), "utf8");
}

it("ships a manifest the browser can install, linked from the real document", () => {
  const manifest = JSON.parse(read("../public/manifest.webmanifest")) as Record<
    string,
    unknown
  >;

  expect(manifest).toMatchObject({
    id: "/",
    name: "Arena64",
    short_name: "Arena64",
    start_url: "/",
    scope: "/",
    display: "standalone",
    lang: "uz",
  });
  expect(manifest.description).toEqual(expect.any(String));

  // A64-026.2 §41.3. These two used to be asserted as the literal
  // `#0a0a0a`, which is a copy of a value that lives in `globals.css` —
  // and when A64-025.9 §18.7 gave both themes a trace of the brand hue,
  // this test failed for a colour rather than for anything to do with
  // installability, which is its subject.
  //
  // What is actually worth holding is that the **splash screen and the
  // browser chrome agree**: the manifest paints behind the icon while the
  // app loads and `theme-color` paints the system UI, and a seam between
  // them is visible on every cold start. So the assertion is that they are
  // the same value, whatever that value becomes.
  const document = read("../index.html");
  const darkThemeColor =
    /<meta name="theme-color" media="\(prefers-color-scheme: dark\)" content="([^"]+)"/.exec(
      document,
    )?.[1];
  expect(darkThemeColor).toMatch(/^#[0-9a-f]{6}$/i);
  expect(manifest.theme_color).toBe(darkThemeColor);
  expect(manifest.background_color).toBe(darkThemeColor);

  // Installability needs a 192 and a 512, and a launcher that masks the
  // icon needs one declared `maskable` — without it the board is cropped.
  const icons = manifest.icons as { src: string; sizes: string; purpose: string }[];
  expect(icons.map((icon) => `${icon.sizes} ${icon.purpose}`)).toEqual(
    expect.arrayContaining(["192x192 any", "512x512 any", "512x512 maskable"]),
  );

  // Declared, and actually there. A manifest naming an icon that 404s is
  // an uninstallable application whose manifest looks perfect.
  const shortcuts = manifest.shortcuts as { url: string; icons: { src: string }[] }[];
  for (const src of [...icons, ...shortcuts.flatMap((s) => s.icons)].map((i) => i.src)) {
    expect(src.startsWith("/"), `${src} must be a same-origin path`).toBe(true);
    expect(statSync(pathTo("../public" + src)).size).toBeGreaterThan(0);
  }

  // §19: stable routes only. A shortcut carrying an id would be a link to
  // one match, frozen into the launcher of everybody who installed it.
  expect(shortcuts.map((shortcut) => shortcut.url)).toEqual([
    "/play",
    "/tournaments",
    "/games/history",
  ]);
  for (const shortcut of shortcuts) {
    expect(shortcut.url).not.toMatch(/[$:?]/);
  }
  // §31: a manifest is world-readable. Nothing user-specific may reach it.
  expect(read("../public/manifest.webmanifest")).not.toMatch(/token|ticket|session/i);

  // §32: the manifest is reachable from the document that actually loads.
  const html = read("../index.html");
  expect(html).toContain('rel="manifest" href="/manifest.webmanifest"');
  expect(html).toContain('rel="apple-touch-icon" href="/icons/apple-touch-icon.png"');
  expect(html).toContain('name="theme-color"');
});

it("precaches the shell only, bounded, and versioned by what it contains", () => {
  // A bundle shaped like the real one: an entry, a chunk it imports
  // statically, its CSS, a lazily-imported route chunk, and a source map.
  const bundle: Record<string, BundleItem> = {
    "index.html": { type: "asset", fileName: "index.html" },
    "assets/index-AAA.js": {
      type: "chunk",
      name: "index",
      fileName: "assets/index-AAA.js",
      isEntry: true,
      imports: ["assets/ui-BBB.js"],
      viteMetadata: { importedCss: new Set(["assets/index-CCC.css"]) },
    },
    "assets/ui-BBB.js": {
      type: "chunk",
      name: "ui",
      fileName: "assets/ui-BBB.js",
      isEntry: false,
      imports: [],
    },
    // Reached only through a dynamic import, so it is not in `imports`.
    "assets/tournament-DDD.js": {
      type: "chunk",
      name: "tournament",
      fileName: "assets/tournament-DDD.js",
      isEntry: false,
      imports: [],
    },
    "assets/index-AAA.js.map": { type: "asset", fileName: "assets/index-AAA.js.map" },
  };

  const urls = precacheUrls(shellAssetsFrom(bundle));

  expect(urls).toEqual(
    expect.arrayContaining([
      "/",
      "/assets/index-AAA.js",
      "/assets/ui-BBB.js",
      "/assets/index-CCC.css",
      "/offline.html",
      "/manifest.webmanifest",
      "/icons/icon-512.png",
    ]),
  );
  // §30: twenty-three route chunks are not the application shell. The
  // runtime cache takes each one the first time a player opens it.
  expect(urls).not.toContain("/assets/tournament-DDD.js");
  expect(urls).not.toContain("/assets/index-AAA.js.map");
  // Bounded: the shell plus seven fixed public files, and nothing that
  // grows with the number of routes.
  expect(urls.length).toBe(11);

  // §25: the version is a fingerprint of the cached content, so an edit to
  // an *unhashed* public file still invalidates the cache — and a rebuild
  // that changed nothing does not.
  const bytes = [Buffer.from("offline v1")];
  expect(cacheVersion(urls, bytes)).toBe(cacheVersion(urls, bytes));
  expect(cacheVersion(urls, bytes)).not.toBe(cacheVersion(urls, [Buffer.from("offline v2")]));
  expect(cacheVersion(urls, bytes)).not.toBe(cacheVersion([...urls, "/extra.js"], bytes));

  // The build refuses to ship a worker that could not register, and one
  // that never had its manifest injected.
  expect(() => assertShippableWorker('import { x } from "./y";\nself.x = x;')).toThrow(
    /module syntax/,
  );
  expect(() => assertShippableWorker("self.urls = __ARENA64_PRECACHE__;")).toThrow(
    /__ARENA64_PRECACHE__/,
  );
  expect(() =>
    assertShippableWorker('self.addEventListener("fetch", () => {});'),
  ).not.toThrow();
});
