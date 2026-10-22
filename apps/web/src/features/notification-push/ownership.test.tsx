import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/shared/api";
import { httpClient } from "@/shared/api/client";
import { env } from "@/shared/config/env";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

import { exclusively } from "./model/exclusive";
import { reconcilePushOwnership } from "./model/ownership";
import { enablePush } from "./model/subscription";

/**
 * A browser lets go of a push subscription the signed-in account does not
 * own — A64-031.E6.
 *
 * The case `release.ts` cannot reach: nobody watched the previous account's
 * session end, so nothing unsubscribed the browser. Driven through the real
 * app — the session provider, its interceptors and `PushReleaseProvider` —
 * with MSW answering the ownership question the way the backend does: from
 * whose token asked and whose row the endpoint is.
 *
 * Like `push.test.tsx`, this proves the flow and nothing about a real push
 * service; the backend half is `apps/api/tests/contract/test_push_notifications.py`.
 */
const url = (path: string) => `${env.VITE_API_URL}${path}`;
const envelope = <T,>(data: T) => ({ data, meta: { request_id: null, correlation_id: null } });

function account(id: string, name: string) {
  return {
    id,
    username: name.toLowerCase(),
    display_name: name,
    email: `${name.toLowerCase()}@example.com`,
    is_active: true,
    is_verified: true,
  };
}

const ALICE = account("019fb9ea-0a0c-7cec-9c5f-4027270a11ce", "Alice");
const BOB = account("019fb9ea-0a0c-7cec-9c5f-402727000b0b", "Bob");

const ENDPOINT = "https://push.example.com/wpush/held-by-this-browser";
const VAPID_PUBLIC =
  "BEwDWIj79dKBncMgwCo17zo12oSzd4MpMA7yuAAf6YTB1iMpZ5XbxBnbuWW6akdWWSa7WO6xaSMPOHDZ521GNPA";

/** Whose session the refresh cookie holds; `null` once it expired. */
let cookie: typeof ALICE | null;
let issued: number;
const expired = new Set<string>();
/** The backend's rows: endpoint → the account it is registered to. */
const rows = new Map<string, string>();
const server = { asked: 0, removals: 0 };

function ownerOf(request: Request): string | null {
  const token = request.headers.get("Authorization")?.replace("Bearer ", "") ?? "";
  if (expired.has(token)) return null;
  return /^token-(.+)-\d+$/.exec(token)?.[1] ?? null;
}

function sessionFor(user: typeof ALICE) {
  issued += 1;
  return HttpResponse.json(envelope({ access_token: `token-${user.id}-${issued}`, user }));
}

function refused(status: number, code: string) {
  return HttpResponse.json(
    { code, message: "x", request_id: null, correlation_id: null },
    { status },
  );
}

interface Browser {
  subscription: PushSubscription | null;
  unsubscribed: number;
}

/** `Notification`, the service worker and `PushManager`, as `push.test.tsx` defines them. */
function installBrowserPush(
  options: { subscribed?: boolean; unsubscribe?: () => Promise<boolean> } = {},
): Browser {
  const browser: Browser = { subscription: null, unsubscribed: 0 };
  const makeSubscription = () =>
    ({
      endpoint: ENDPOINT,
      getKey: (name: string) =>
        name === "p256dh"
          ? new Uint8Array(65).fill(4).buffer
          : new Uint8Array(16).fill(7).buffer,
      unsubscribe:
        options.unsubscribe ??
        (() => {
          browser.unsubscribed += 1;
          browser.subscription = null;
          return Promise.resolve(true);
        }),
    }) as unknown as PushSubscription;

  if (options.subscribed) browser.subscription = makeSubscription();

  const pushManager = {
    getSubscription: () => Promise.resolve(browser.subscription),
    subscribe: () => {
      browser.subscription = makeSubscription();
      return Promise.resolve(browser.subscription);
    },
  };
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: {
      ready: Promise.resolve({ pushManager }),
      getRegistration: () => Promise.resolve({ active: {}, pushManager }),
    },
  });
  Object.defineProperty(globalThis, "PushManager", {
    configurable: true,
    writable: true,
    value: function PushManagerStub() {},
  });
  Object.defineProperty(globalThis, "Notification", {
    configurable: true,
    writable: true,
    value: { permission: "granted", requestPermission: () => Promise.resolve("granted") },
  });
  return browser;
}

/**
 * Waits for every change to the subscription already queued — the ownership
 * check included — to finish. The only way to assert that something did
 * **not** happen without guessing at a delay.
 */
function settled(): Promise<void> {
  return exclusively(() => Promise.resolve());
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  httpClient.interceptors.request.clear();
  httpClient.interceptors.response.clear();
  cookie = ALICE;
  issued = 0;
  expired.clear();
  rows.clear();
  rows.set(ENDPOINT, ALICE.id);
  server.asked = 0;
  server.removals = 0;

  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      cookie === null ? refused(401, "invalid_session") : sessionFor(cookie),
    ),
    http.post(url("/auth/browser/login"), () => sessionFor(BOB)),
    http.get(url("/private"), ({ request }) =>
      ownerOf(request) === null
        ? refused(401, "expired_token")
        : HttpResponse.json(envelope({ ok: true })),
    ),
    http.post(url("/notifications/push/subscriptions/ownership"), async ({ request }) => {
      server.asked += 1;
      const owner = ownerOf(request);
      if (owner === null) return refused(401, "expired_token");
      const { endpoint } = (await request.json()) as { endpoint: string };
      return HttpResponse.json(envelope({ owned: rows.get(endpoint) === owner }));
    }),
    http.post(url("/notifications/push/subscriptions/remove"), () => {
      server.removals += 1;
      return new HttpResponse(null, { status: 204 });
    }),
  );
});

afterEach(() => {
  // Not `vi.unstubAllGlobals()` — see `push.test.tsx`.
  Reflect.deleteProperty(globalThis, "Notification");
  Reflect.deleteProperty(globalThis, "PushManager");
  Reflect.deleteProperty(navigator, "serviceWorker");
});

describe("another account's subscription", () => {
  it("is let go when somebody else opens the browser signed in", async () => {
    // A's session lapsed and the browser was closed; it reopens with B's
    // session. Also every subscription made before this check existed.
    const browser = installBrowserPush({ subscribed: true });
    cookie = BOB;

    renderApp({ path: "/" });

    await vi.waitFor(() => expect(browser.unsubscribed).toBe(1));
  });

  it("is let go when somebody signs in after the previous session expired", async () => {
    const browser = installBrowserPush({ subscribed: true });
    cookie = null;
    const person = userEvent.setup();
    renderApp({ path: "/login" });

    const submit = await screen.findByRole("button", { name: /sign in/i });
    const form = within(submit.closest("form") as HTMLElement);
    await person.type(form.getByLabelText(/email/i, { selector: "input" }), "bob");
    await person.type(form.getByLabelText(/password/i, { selector: "input" }), "x");
    await person.click(submit);

    await vi.waitFor(() => expect(browser.unsubscribed).toBe(1));
  });

  it("is never removed from the server, so the owner's other devices keep theirs", async () => {
    // Removal is scoped to the owner, and the owner is not signed in. The
    // row is retired by the push service's 410 once the browser lets go.
    const browser = installBrowserPush({ subscribed: true });
    cookie = BOB;

    renderApp({ path: "/" });
    await vi.waitFor(() => expect(browser.unsubscribed).toBe(1));

    expect(server.removals).toBe(0);
  });
});

describe("the account's own subscription", () => {
  it("is kept", async () => {
    const browser = installBrowserPush({ subscribed: true });

    renderApp({ path: "/" });
    await vi.waitFor(() => expect(server.asked).toBe(1));
    await settled();

    expect(browser.unsubscribed).toBe(0);
  });

  it("is not asked about again on an ordinary token refresh", async () => {
    installBrowserPush({ subscribed: true });
    renderApp({ path: "/" });
    await vi.waitFor(() => expect(server.asked).toBe(1));
    expired.add(`token-${ALICE.id}-1`);

    await api.get("/private");
    await settled();

    expect(server.asked).toBe(1);
  });
});

describe("failing closed", () => {
  it("lets go of the subscription when the question cannot be answered", async () => {
    // A missed notification on the owner's own browser is recovered by
    // enabling push again; another account's on this screen is not.
    mswServer.use(
      http.post(url("/notifications/push/subscriptions/ownership"), () =>
        refused(503, "service_unavailable"),
      ),
    );
    const browser = installBrowserPush({ subscribed: true });

    renderApp({ path: "/" });

    await vi.waitFor(() => expect(browser.unsubscribed).toBe(1));
  });

  it("reports a browser that will not unsubscribe, rather than swallowing it", async () => {
    installBrowserPush({
      subscribed: true,
      unsubscribe: () => Promise.reject(new Error("no")),
    });
    cookie = BOB;

    renderApp({ path: "/" });

    await vi.waitFor(() =>
      expect(console.error).toHaveBeenCalledWith("[arena64]", expect.any(Error), {
        scope: "push-ownership",
      }),
    );
  });
});

describe("nothing to check", () => {
  it("asks nothing when this browser holds no subscription", async () => {
    installBrowserPush();

    renderApp({ path: "/" });
    await screen.findByText("Alice");
    await settled();

    expect(server.asked).toBe(0);
  });

  it("asks nothing on a browser that cannot receive push", async () => {
    Object.defineProperty(navigator, "serviceWorker", { configurable: true, value: {} });

    renderApp({ path: "/" });
    await screen.findByText("Alice");
    await settled();

    expect(server.asked).toBe(0);
  });
});

describe("a check racing an enable", () => {
  /**
   * Web Locks, reduced to what `exclusive.ts` uses — one named queue — plus
   * what the test needs to see: whether a request is blocked behind a task
   * holding the lock, rather than a guess at how long to give it.
   */
  function installLocks() {
    const locks = { waiting: 0, holding: 0 };
    let held: Promise<unknown> = Promise.resolve();
    const request = (_name: string, task: () => Promise<unknown>) => {
      locks.waiting += 1;
      const run = held.then(async () => {
        locks.waiting -= 1;
        locks.holding += 1;
        try {
          return await task();
        } finally {
          locks.holding -= 1;
        }
      });
      held = run.catch(() => undefined);
      return run;
    };
    Object.defineProperty(navigator, "locks", { configurable: true, value: { request } });
    return locks;
  }

  afterEach(() => {
    Reflect.deleteProperty(navigator, "locks");
  });

  it("waits until the new subscription is stored, and keeps it", async () => {
    // Between the browser subscribing and the backend storing the endpoint,
    // the answer is "not yours" — a check that ran there would unsubscribe
    // the device the person is enabling.
    const browser = installBrowserPush();
    const locks = installLocks();
    rows.clear();
    // Below the app, so no session: the server answers from its rows alone.
    mswServer.use(
      // Answered from the rows as they stand when the request **arrives**.
      http.post(url("/notifications/push/subscriptions/ownership"), () => {
        server.asked += 1;
        return HttpResponse.json(envelope({ owned: rows.has(ENDPOINT) }));
      }),
    );
    let store: () => void = () => undefined;
    const stored = new Promise<void>((resolve) => {
      store = resolve;
    });

    const enabling = enablePush(VAPID_PUBLIC, async () => {
      await stored;
      rows.set(ENDPOINT, ALICE.id);
    });
    await vi.waitFor(() => expect(browser.subscription).not.toBeNull());
    const checking = reconcilePushOwnership(() => true);
    // Either the check is blocked behind the enable, or — if nothing made
    // it wait — it has already asked about a subscription not yet stored.
    await vi.waitFor(() =>
      expect(server.asked > 0 || (locks.waiting > 0 && locks.holding > 0)).toBe(true),
    );
    store();
    await Promise.all([enabling, checking]);

    expect(browser.unsubscribed).toBe(0);
  });
});
