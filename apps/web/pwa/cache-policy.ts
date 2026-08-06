/**
 * What the service worker is allowed to touch — A64-020.9 §9, §10, §31.
 *
 * ## Why this is a module and not a `switch` inside the worker
 *
 * The cache policy is the security-relevant half of a service worker: a
 * worker that caches one authenticated response has published one user's
 * private data to whoever uses the device next. That decision has to be
 * *directly* testable, without a `ServiceWorkerGlobalScope` to fake and
 * without evaluating a bundled artefact, which is what makes it a pure
 * function in its own file. `service-worker.ts` imports it and does the
 * I/O; the rules live here.
 *
 * ## The rule, in one sentence
 *
 * **Cache only what a signed-out stranger could have fetched anyway.**
 * Every hashed build asset qualifies; nothing on `/api` or `/ws` does, and
 * nothing on another origin does. There is no allow-list of "safe"
 * authenticated reads in this phase — §10's conservative option — because
 * proving per-user isolation inside a cache shared by every session on the
 * device is a claim this codebase cannot currently make. TanStack Query
 * owns in-session data, in memory, where signing out clears it.
 */

/**
 * Every cache this application owns starts with this.
 *
 * Scoped so `activate` can delete *Arena64's* stale caches and only those.
 * Another application on the same origin — a staging build, a docs site —
 * owns its caches and this worker must not touch them (§25).
 */
export const CACHE_PREFIX = "arena64-";

/** The application shell. Replaced wholesale when the build changes. */
export function precacheName(version: string): string {
  return `${CACHE_PREFIX}precache-${version}`;
}

/**
 * Hashed assets fetched on demand — the lazy route chunks, mostly.
 *
 * Separate from the precache because it fills up as a player navigates
 * rather than at install, and because it is the one cache that needs a
 * bound (§25). Versioned alongside the precache so a new build starts from
 * an empty one instead of inheriting entries whose siblings are gone.
 */
export function assetCacheName(version: string): string {
  return `${CACHE_PREFIX}assets-${version}`;
}

/**
 * The bound on the runtime asset cache.
 *
 * Twenty-three routes plus their shared chunks is well under this, so the
 * limit is not a working constraint — it is the guarantee that a bug in
 * the classifier cannot grow the cache without end (§25, CLAUDE.md §10.5).
 * Trimmed oldest-first, which for immutable hashed assets means the
 * least recently *added*, and a trimmed asset costs one refetch.
 */
export const ASSET_CACHE_MAX_ENTRIES = 64;

/**
 * Prefixes the worker never reads from or writes to a cache, whatever else
 * the rules below would say.
 *
 * `/api` covers every authenticated read and write, including the three the
 * task names explicitly: `POST /api/v1/auth/browser/refresh`,
 * `POST /api/v1/auth/ws-ticket`, and every mutation. `/ws` is the gateway,
 * which is an upgrade handshake rather than a resource — a worker that
 * answered it from a cache would break the socket rather than accelerate
 * it (§13).
 */
export const NEVER_CACHED_PREFIXES = ["/api", "/ws"] as const;

/** Where Vite emits hashed build output. Immutable by construction. */
export const ASSET_PATH_PREFIX = "/assets/";

/**
 * How the worker should answer one request.
 *
 * `"network"` means **untouched** — the worker does not call
 * `respondWith` at all and the browser's own stack handles it, so a
 * request classified this way cannot be cached, delayed, or observed by
 * anything here.
 */
export type Handling = "precache" | "navigate" | "asset" | "network";

/** The parts of a `Request` this decision depends on, and nothing else. */
export interface ClassifiableRequest {
  readonly method: string;
  readonly url: string;
  /** `"navigate"` for a document request. */
  readonly mode: string;
}

export interface PolicyContext {
  /** The origin the worker was served from. Anything else is third-party. */
  readonly origin: string;
  /** Path-and-query keys of the precached shell, exactly as installed. */
  readonly precached: ReadonlySet<string>;
}

/**
 * Decides what happens to one request. **Ordered, and the order matters.**
 *
 * The never-cached check comes before the navigation check on purpose: a
 * player who opens `/api/v1/docs` in a tab issues a *navigation* to the
 * API, and answering it with the application shell would replace a real
 * response with a lie.
 */
export function classify(request: ClassifiableRequest, context: PolicyContext): Handling {
  // A cache is a map keyed by URL. A POST, PATCH or DELETE is not a
  // lookup, and the Cache API declines to store one anyway — stated here
  // so the reason is in the policy rather than in a platform footnote.
  if (request.method !== "GET") return "network";

  const url = parseUrl(request.url);
  if (url === null) return "network";

  // Avatars, CDNs, analytics, anything at all: another origin's caching is
  // that origin's decision, expressed in its own headers.
  if (url.origin !== context.origin) return "network";

  if (isNeverCached(url.pathname)) return "network";

  if (request.mode === "navigate") return "navigate";

  if (context.precached.has(url.pathname)) return "precache";

  if (isHashedAsset(url.pathname)) return "asset";

  return "network";
}

export function isNeverCached(pathname: string): boolean {
  return NEVER_CACHED_PREFIXES.some(
    (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`),
  );
}

/**
 * A build artefact whose name contains its own content hash.
 *
 * Source maps are excluded: only a developer's devtools ever asks for one,
 * and caching them would spend a player's storage on bytes they will never
 * read.
 */
export function isHashedAsset(pathname: string): boolean {
  return pathname.startsWith(ASSET_PATH_PREFIX) && !pathname.endsWith(".map");
}

function parseUrl(value: string): URL | null {
  try {
    return new URL(value);
  } catch {
    // A request whose URL will not parse is not one this worker can
    // reason about, so it goes to the network untouched.
    return null;
  }
}

// --- the page-to-worker message contract ------------------------------------

/**
 * The only message this worker accepts — §31.
 *
 * One string constant, matched exactly, carrying no payload. A worker that
 * branched on arbitrary message content would be an execution seam
 * reachable from any script on the origin; this one can do exactly one
 * thing, which the user already asked for by pressing Update.
 */
export const SKIP_WAITING_MESSAGE = "arena64/skip-waiting";

export function isSkipWaitingMessage(data: unknown): boolean {
  return (
    typeof data === "object" &&
    data !== null &&
    (data as { type?: unknown }).type === SKIP_WAITING_MESSAGE
  );
}
