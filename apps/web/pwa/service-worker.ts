import {
  ASSET_CACHE_MAX_ENTRIES,
  assetCacheName,
  CACHE_PREFIX,
  classify,
  isSkipWaitingMessage,
  precacheName,
} from "./cache-policy";

/**
 * Arena64's one service worker — A64-020.9 §8.
 *
 * Authored here, compiled by Vite as a second Rollup entry, and emitted as
 * `/sw.js` at the site root so its scope is exactly the application's
 * scope and no broader (§31). `pwa/vite-plugin.ts` injects the two values
 * this file cannot know at author time: the precache manifest and the
 * version derived from it.
 *
 * **There is no second worker.** No generated worker, no library worker,
 * no dev worker — §8's "one service worker" is a property of the build,
 * asserted by `pwa/pwa-build.test.ts`.
 *
 * ## What it does, in full
 *
 *     install    fetch the shell and store it. Does *not* skip waiting.
 *     activate   drop Arena64's stale caches, then claim open pages.
 *     fetch      shell from cache, hashed assets on demand, everything
 *                else untouched.
 *     message    one message, one action: skip waiting, on the user's word.
 *
 * ## What it deliberately does not do
 *
 * It never caches an API response, never stores a token, never queues a
 * write, never touches `/ws`, and never reads a request body. A request the
 * policy classifies as `"network"` is not merely fetched by this worker —
 * it is *not handled at all*, so the browser's own stack answers it as
 * though no worker were installed (§10, §12, §13).
 *
 * ## Why the types are declared locally
 *
 * A service worker's globals live in TypeScript's `webworker` lib, which
 * cannot share a program with `dom` — the two define hundreds of the same
 * names. A second `tsconfig` was the alternative and costs a second
 * `typecheck` invocation plus a second place for compiler options to
 * drift. Declaring the six shapes this file actually uses is smaller, and
 * it documents the platform surface the worker depends on.
 */

interface ExtendableEvent {
  waitUntil(promise: Promise<unknown>): void;
}

interface FetchEvent extends ExtendableEvent {
  readonly request: Request;
  respondWith(response: Response | Promise<Response>): void;
}

interface ExtendableMessageEvent extends ExtendableEvent {
  readonly data: unknown;
}

interface ServiceWorkerClients {
  claim(): Promise<void>;
}

interface ServiceWorkerScope {
  readonly location: { readonly origin: string };
  readonly clients: ServiceWorkerClients;
  skipWaiting(): Promise<void>;
  addEventListener(
    type: "install" | "activate",
    listener: (event: ExtendableEvent) => void,
  ): void;
  addEventListener(type: "fetch", listener: (event: FetchEvent) => void): void;
  addEventListener(type: "message", listener: (event: ExtendableMessageEvent) => void): void;
}

const worker = globalThis as unknown as ServiceWorkerScope;

/**
 * Replaced at build time by `pwa/vite-plugin.ts`.
 *
 * `declare const` rather than an import: these are the build's answer, not
 * a module's, and there is no honest value for them in source. The plugin
 * fails the build if either identifier is missing from the emitted chunk,
 * so a worker can never ship with the placeholder in it.
 */
declare const __ARENA64_PRECACHE__: readonly string[];
declare const __ARENA64_VERSION__: string;

const PRECACHE_URLS: readonly string[] = __ARENA64_PRECACHE__;
const VERSION: string = __ARENA64_VERSION__;

const PRECACHE = precacheName(VERSION);
const ASSET_CACHE = assetCacheName(VERSION);

/** The application shell — one document, serving every client route. */
const SHELL_URL = "/";
/** The last resort, when even the shell is not cached. */
const OFFLINE_URL = "/offline.html";

const PRECACHED_PATHS = new Set(PRECACHE_URLS);

/**
 * **`ignoreVary` is not optional here.**
 *
 * Vite's preview server — and most CDNs and reverse proxies — answer with
 * `Vary: Origin`. The Cache API honours `Vary` by default, so a response
 * stored from this worker's own `new Request(url)`, which carries no
 * `Origin`, never matches the browser's request for a module script, which
 * carries one (Vite marks its script tags `crossorigin`). Every precache
 * lookup misses, every offline load falls through to a network that is not
 * there, and the shell "does not work offline" for a reason nothing in the
 * cache contents suggests. This cost one debugging session and is the
 * reason the offline assertion in `tests/e2e/pwa.spec.ts` exists.
 *
 * Ignoring it is correct rather than merely convenient: everything this
 * worker caches is a static file with exactly one representation, and the
 * `Vary: Origin` on it is a CORS artefact rather than a statement that the
 * bytes differ per caller.
 */
const MATCH_OPTIONS: CacheQueryOptions = { ignoreVary: true };

worker.addEventListener("install", (event) => {
  // **No `skipWaiting()` here.** A worker that activated on install would
  // swap the code under a player mid-game — §14 gives that decision to the
  // person playing, and this is where refusing to take it is enforced.
  event.waitUntil(precacheShell());
});

worker.addEventListener("activate", (event) => {
  event.waitUntil(activate());
});

worker.addEventListener("fetch", (event) => {
  const handling = classify(event.request, {
    origin: worker.location.origin,
    precached: PRECACHED_PATHS,
  });

  // `"network"` returns *without* calling `respondWith`, which hands the
  // request back to the browser untouched. That is the difference between
  // "we fetch it for you" and "we are not involved", and for `/api` and
  // `/ws` only the second is acceptable.
  if (handling === "network") return;

  if (handling === "navigate") {
    event.respondWith(respondToNavigation(event.request));
    return;
  }

  if (handling === "precache") {
    event.respondWith(fromPrecache(event.request));
    return;
  }

  event.respondWith(fromAssetCache(event.request));
});

worker.addEventListener("message", (event) => {
  // Exactly one message is understood and everything else is ignored in
  // silence — §31. There is no branch here that can be talked into doing
  // something the user did not press a button for.
  if (!isSkipWaitingMessage(event.data)) return;
  event.waitUntil(worker.skipWaiting());
});

async function precacheShell(): Promise<void> {
  const cache = await caches.open(PRECACHE);
  // `cache: "reload"` bypasses the HTTP cache: precaching a copy the
  // browser happened to be holding would install a shell older than the
  // build that asked for it, which is the one thing precaching must not do.
  await cache.addAll(PRECACHE_URLS.map((url) => new Request(url, { cache: "reload" })));
}

async function activate(): Promise<void> {
  const names = await caches.keys();
  await Promise.all(
    names
      // **Arena64's caches only.** Another application on this origin owns
      // its own storage, and a worker that cleaned up on its behalf would
      // be a bug in two products at once (§25).
      .filter(
        (name) => name.startsWith(CACHE_PREFIX) && name !== PRECACHE && name !== ASSET_CACHE,
      )
      .map((name) => caches.delete(name)),
  );

  // Take over pages that were open when this worker activated. Without it
  // the tab that just pressed Update would keep talking to the worker it
  // replaced until its next navigation.
  await worker.clients.claim();
}

/**
 * Any in-app route, answered by the one cached document.
 *
 * The router owns the path; this owns the shell. That is why a single
 * cached `/` serves `/play`, `/games/history` and everything else — and
 * why no route is ever cached as an HTML snapshot (§9): there is nothing
 * user-specific in this document to leak, because it contains no data at
 * all.
 */
async function respondToNavigation(request: Request): Promise<Response> {
  const cache = await caches.open(PRECACHE);
  const shell = await cache.match(SHELL_URL, MATCH_OPTIONS);
  if (shell !== undefined) return shell;

  try {
    return await fetch(request);
  } catch {
    const offline = await cache.match(OFFLINE_URL, MATCH_OPTIONS);
    // A 503 with no body rather than a message: the user-facing text lives
    // in `offline.html`, and inventing a second one here would be an
    // internal string on a screen (CLAUDE.md §9.7).
    return offline ?? new Response(null, { status: 503 });
  }
}

async function fromPrecache(request: Request): Promise<Response> {
  const cache = await caches.open(PRECACHE);
  const hit = await cache.match(request, MATCH_OPTIONS);
  // A miss here means the entry was evicted under storage pressure. The
  // network is the correct answer, not a failure.
  return hit ?? fetch(request);
}

/**
 * A hashed build asset — a lazy route chunk, in practice.
 *
 * Cache-first, because the name contains the content hash: a URL that
 * resolves once can never resolve to different bytes, so revalidation
 * would be a round trip that cannot change the answer. §30 asks for
 * on-demand caching rather than precaching twenty-three routes a visitor
 * may never open, and this is it.
 */
async function fromAssetCache(request: Request): Promise<Response> {
  const cache = await caches.open(ASSET_CACHE);
  const hit = await cache.match(request, MATCH_OPTIONS);
  if (hit !== undefined) return hit;

  const response = await fetch(request);
  // Only a complete, same-origin, successful response is worth storing. An
  // opaque or partial one cached here would be indistinguishable from the
  // real thing on the next load and impossible to invalidate.
  if (response.ok && response.type === "basic") {
    await cache.put(request, response.clone());
    await trim(cache);
  }
  return response;
}

/**
 * Keeps the runtime cache bounded — §25.
 *
 * `keys()` answers in insertion order, so the front of the list is the
 * oldest entry. Trimming costs one refetch of an immutable asset, which is
 * the cheapest thing this application can lose.
 */
async function trim(cache: Cache): Promise<void> {
  const keys = await cache.keys();
  const excess = keys.length - ASSET_CACHE_MAX_ENTRIES;
  if (excess <= 0) return;
  await Promise.all(keys.slice(0, excess).map((key) => cache.delete(key)));
}
