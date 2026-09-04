import { expect, test } from "@playwright/test";

/**
 * Arena64 as an installed application, against the **built** app in a real
 * browser — A64-020.9 §28.10, §32.
 *
 * Everything else in this phase is asserted in jsdom, which has no Cache
 * Storage, no service worker, and no notion of being offline. This is the
 * only place the worker actually installs, actually claims the page, and
 * actually answers a navigation from a cache — and the only place the
 * security claim in §31 can be checked against storage rather than against
 * the code that writes to it.
 *
 * Unauthenticated on purpose. The point of the last assertion is that an
 * offline visitor is told the session could not be checked, rather than
 * being shown somebody's cached data or bounced to a sign-in page they
 * cannot reach.
 */
test("installs a worker, serves the shell offline, and caches nothing private", async ({
  page,
  context,
}) => {
  await page.goto("/");
  // A level-one heading, unnamed — the twin of `shell.spec.ts`'s probe and
  // red for the same reason since A64-026.1: `/` is the landing page for a
  // visitor, whose `h1` is the hero's sentence. What this spec is about is
  // the service worker, so it waits for the shell to be there and does not
  // name whichever page currently answers `/`.
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();

  // The manifest is linked from the document *and* served — a `<link>` to
  // a 404 is an uninstallable app whose HTML looks correct.
  await expect(page.locator('link[rel="manifest"]')).toHaveAttribute(
    "href",
    "/manifest.webmanifest",
  );
  const manifest = await page.request.get("/manifest.webmanifest");
  expect(manifest.status()).toBe(200);
  expect((await manifest.json()).name).toBe("Arena64");

  // The worker installs, activates and claims this page — §8. Without
  // `clients.claim()` the first visit would be uncontrolled, and the
  // offline reload below would reach the network instead of the cache.
  await page.waitForFunction(() => navigator.serviceWorker.controller !== null, null, {
    timeout: 15_000,
  });
  const scope = await page.evaluate(async () => {
    const registration = await navigator.serviceWorker.ready;
    return registration.scope;
  });
  expect(new URL(scope).pathname).toBe("/");

  // One more visit **while online and controlled**. The home route arrives
  // as a lazy chunk (§30: route chunks are cached on demand, not
  // precached), and the first visit happened before this worker existed —
  // so this is the load that puts it in the runtime cache. It is also the
  // honest journey: offline works for a returning visitor, and A64-020.9
  // never claims a first visit made offline can run the application.
  await page.reload();
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();

  // --- offline ------------------------------------------------------------
  await context.setOffline(true);
  await page.reload();

  // The shell came from the precache and the route chunk from the runtime
  // cache. Nothing else in this application could have answered either.
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();

  // The offline *notice* is deliberately not asserted here.
  // `context.setOffline(true)` disconnects the network through CDP but
  // leaves `navigator.onLine` reporting `true`, so the one signal
  // `shared/pwa/connectivity.ts` reads never changes — the notice is
  // covered in jsdom, where that flag can be stubbed
  // (`src/widgets/pwa/notices.test.tsx`). Asserting it here would mean
  // weakening the component to satisfy the emulator.

  // §11, §12: a protected read is **not** answered from anywhere. The
  // worker does not handle `/api` at all, so an offline request fails as
  // it would with no worker installed — which is what stops private data
  // from being presented as current.
  const protectedRead = await page.evaluate(async () => {
    try {
      const response = await fetch("/api/v1/profile/me");
      return `answered-${response.status}`;
    } catch {
      return "not-answered";
    }
  });
  expect(protectedRead).toBe("not-answered");

  // --- what is in storage, and what must never be -------------------------
  const stored = await page.evaluate(async () => {
    const names = await caches.keys();
    const urls: string[] = [];
    for (const name of names) {
      const cache = await caches.open(name);
      for (const request of await cache.keys()) urls.push(request.url);
    }
    return { names, urls };
  });

  // §25: Arena64's caches carry Arena64's prefix, so cleanup can never
  // reach another application sharing this origin.
  expect(stored.names.length).toBeGreaterThan(0);
  for (const name of stored.names) expect(name.startsWith("arena64-")).toBe(true);

  // §10, §12, §31: not one authenticated response, not one token, not the
  // ws ticket. The shell and hashed build assets, and nothing else.
  const paths = stored.urls.map((url) => new URL(url).pathname);
  expect(paths).toContain("/");
  expect(paths.some((path) => path.startsWith("/api"))).toBe(false);
  expect(paths.some((path) => path.startsWith("/ws"))).toBe(false);
  expect(stored.urls.some((url) => /ticket|token|refresh/i.test(url))).toBe(false);
  for (const path of paths) {
    expect(
      path === "/" ||
        path.startsWith("/assets/") ||
        path.startsWith("/icons/") ||
        path.endsWith(".html") ||
        path.endsWith(".webmanifest"),
      `${path} is cached but is not a shell asset`,
    ).toBe(true);
  }
});
