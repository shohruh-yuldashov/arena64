import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { env } from "@/shared/config/env";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * The push section of `/settings/notifications` — A64-021.6 §20, §21, §30.
 *
 * Through the **real router**, so route registration, both guards and the
 * session provider are exercised together. What is substituted is the HTTP
 * layer (MSW) and the browser's Push API — which `jsdom` does not implement
 * at all, so there is nothing to intercept and everything to define.
 *
 * ## What a mocked `PushManager` does and does not prove — §28
 *
 * It proves the **flow**: that permission is requested only on a click, that
 * the subscription is serialised into the three fields the API accepts, that
 * the preference is enabled only after the subscription is stored, and that
 * each of §20's states renders its own instruction.
 *
 * It proves **nothing** about external Web Push delivery. No push service is
 * contacted, no message is encrypted, and a browser that accepted this
 * subscription might still reject the real one. The bytes are covered where
 * they can be — `apps/api/tests/unit/test_web_push_protocol.py` decrypts a
 * real payload as a user agent would — and the transport is covered by
 * `test_push_notifications.py` against a stub service.
 */

const url = (path: string) => `${env.VITE_API_URL}${path}`;
const envelope = <T,>(data: T) => ({ data, meta: { request_id: null, correlation_id: null } });

const VAPID_PUBLIC =
  "BEwDWIj79dKBncMgwCo17zo12oSzd4MpMA7yuAAf6YTB1iMpZ5XbxBnbuWW6akdWWSa7WO6xaSMPOHDZ521GNPA";

const USER = {
  id: "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
  username: "player",
  display_name: null,
  email: "player@example.com",
  is_active: true,
  is_verified: true,
};

/** The preference matrix, with push off unless a test says otherwise. */
function preferences(pushEnabled = false) {
  return {
    settings: [
      {
        category: "tournament",
        channel: "in_app",
        enabled: true,
        editable: true,
        locked_reason: null,
      },
      {
        category: "tournament",
        channel: "push",
        enabled: pushEnabled,
        editable: true,
        locked_reason: null,
      },
    ],
  };
}

interface BrowserPush {
  permission: NotificationPermission;
  subscription: PushSubscription | null;
  requested: number;
  subscribeCalls: unknown[];
  unsubscribed: number;
}

/**
 * Defines the browser APIs `jsdom` does not have.
 *
 * `Notification`, `navigator.serviceWorker` and `PushManager` all have to be
 * created rather than spied on. The shape is narrowed to what
 * `model/subscription.ts` actually touches — a fuller fake would be a second
 * implementation of the Push API, which is not what is under test.
 */
function installBrowserPush(
  options: { permission?: NotificationPermission; subscribed?: boolean } = {},
): BrowserPush {
  const state: BrowserPush = {
    permission: options.permission ?? "default",
    subscription: null,
    requested: 0,
    subscribeCalls: [],
    unsubscribed: 0,
  };

  const makeSubscription = () =>
    ({
      endpoint: "https://push.example.com/wpush/abc123",
      getKey: (name: string) =>
        name === "p256dh"
          ? new Uint8Array(65).fill(4).buffer
          : new Uint8Array(16).fill(7).buffer,
      unsubscribe: () => {
        state.unsubscribed += 1;
        state.subscription = null;
        return Promise.resolve(true);
      },
    }) as unknown as PushSubscription;

  if (options.subscribed) state.subscription = makeSubscription();

  const notification = {
    permission: state.permission,
    // `Promise.resolve` rather than `async`, throughout this fake: these
    // methods have nothing to await, and the lint rule that notices is
    // right — an `async` with no `await` is a signature promising work it
    // does not do.
    requestPermission: () =>
      Promise.resolve(
        ((): NotificationPermission => {
          state.requested += 1;
          // A browser that has already been answered does not ask again; it
          // returns the stored answer. Reproduced because §7's "do not keep
          // re-prompting" depends on it.
          return state.permission === "default" ? "granted" : state.permission;
        })(),
      ),
  };
  Object.defineProperty(globalThis, "Notification", {
    configurable: true,
    writable: true,
    value: notification,
  });

  const pushManager = {
    getSubscription: () => Promise.resolve(state.subscription),
    subscribe: (subscribeOptions: unknown) => {
      state.subscribeCalls.push(subscribeOptions);
      state.subscription = makeSubscription();
      return Promise.resolve(state.subscription);
    },
  };

  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: {
      ready: Promise.resolve({ pushManager }),
      // Both, because the two paths use different ones deliberately:
      // `ready` waits for an **active** worker and is right for subscribing,
      // and `getRegistration` resolves to `undefined` when there is none
      // and is the only one safe on the read path — `ready` never settles
      // without a registration, which on sign-out is a hang.
      getRegistration: () => Promise.resolve({ pushManager }),
    },
  });
  Object.defineProperty(globalThis, "PushManager", {
    configurable: true,
    writable: true,
    value: function PushManagerStub() {},
  });

  return state;
}

function api({ available = true, deviceCount = 0, pushEnabled = false } = {}) {
  const patched: unknown[] = [];
  const registered: unknown[] = [];
  const removed: unknown[] = [];

  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: USER })),
    ),
    http.get(url("/notifications/preferences"), () =>
      HttpResponse.json(envelope(preferences(pushEnabled))),
    ),
    http.patch(url("/notifications/preferences"), async ({ request }) => {
      patched.push(await request.json());
      return HttpResponse.json(envelope(preferences(true)));
    }),
    http.get(url("/notifications/push/status"), () =>
      HttpResponse.json(
        envelope({
          available,
          vapid_public_key: available ? VAPID_PUBLIC : null,
          device_count: deviceCount,
        }),
      ),
    ),
    http.post(url("/notifications/push/subscriptions"), async ({ request }) => {
      registered.push(await request.json());
      return HttpResponse.json(envelope({ id: "019fb9ea-0000-7000-8000-000000000001" }), {
        status: 201,
      });
    }),
    http.post(url("/notifications/push/subscriptions/remove"), async ({ request }) => {
      removed.push(await request.json());
      return new HttpResponse(null, { status: 204 });
    }),
  );

  return { patched, registered, removed };
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  // **Not `vi.unstubAllGlobals()`.** `shared/test/setup.ts` installs
  // `matchMedia` with `vi.stubGlobal` once for the whole run, and unstubbing
  // everything removes it — after which every subsequent render in this file
  // throws inside the theme provider, for a reason that has nothing to do
  // with push. Only what this file defined is removed.
  Reflect.deleteProperty(globalThis, "Notification");
  Reflect.deleteProperty(globalThis, "PushManager");
  Reflect.deleteProperty(navigator, "serviceWorker");
});

describe("the states §20 forbids compressing", () => {
  it("tells an unsupported browser that it cannot, and offers nothing", async () => {
    // No `PushManager` and no `Notification` — an older browser, or Safari
    // before 16.4. Nothing to press, and the reason said out loud rather
    // than a disabled switch somebody would file a bug about.
    Object.defineProperty(navigator, "serviceWorker", { configurable: true, value: {} });
    api();
    renderApp({ path: "/settings/notifications" });

    expect(await screen.findByText(/browser cannot receive push/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: /enable push/i })).not.toBeInTheDocument();
  });

  it("tells a denied browser to change its own settings, and offers nothing", async () => {
    // The page **cannot** re-prompt once somebody has refused; the browser
    // will not ask again. A button here would do nothing, which teaches
    // people the feature is broken rather than that they turned it off.
    installBrowserPush({ permission: "denied" });
    api();
    renderApp({ path: "/settings/notifications" });

    expect(await screen.findByText(/blocked notifications/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: /enable push/i })).not.toBeInTheDocument();
  });

  it("says so when the server cannot send, rather than offering a switch", async () => {
    // §6. A deployment with no VAPID key pair. The browser supports push
    // perfectly well and there is still nothing to turn on — the two facts
    // are separate, and a UI keyed only on browser support would offer a
    // button whose backend refuses it.
    installBrowserPush();
    api({ available: false });
    renderApp({ path: "/settings/notifications" });

    expect(await screen.findByText(/not available on this server/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: /enable push/i })).not.toBeInTheDocument();
  });
});

describe("recovery when the permission is already granted", () => {
  it("offers the button and re-subscribes without asking again", async () => {
    // **§8.** Permission granted, subscription gone — the browser was told
    // yes at some point and the subscription has since been lost: site data
    // cleared, a service worker update, a push service rotating endpoints.
    //
    // This state is invisible from the server (which sees a device count of
    // zero, same as somebody who never enabled it) and from the permission
    // alone (which says yes). Only the pair distinguishes it, and without a
    // way out the person is stuck: the browser will never prompt again, so
    // there is no dialog to reach.
    const browser = installBrowserPush({ permission: "granted", subscribed: false });
    const calls = api();
    renderApp({ path: "/settings/notifications" });

    expect(await screen.findByText(/not receiving push notifications/i)).toBeVisible();
    await userEvent.click(await screen.findByRole("button", { name: /enable push/i }));

    await waitFor(() => expect(calls.registered).toHaveLength(1));
    // Subscribed afresh, and the prompt was **not** shown a second time —
    // `requestPermission` on an already-answered browser returns the stored
    // answer rather than asking, which is why re-requesting is harmless here
    // and why the count is what proves the recovery worked.
    expect(browser.subscribeCalls).toHaveLength(1);
  });
});

describe("enabling", () => {
  it("asks, subscribes, registers, and only then turns the preference on", async () => {
    // **§21's ordering, which is the claim this test exists for.** A
    // preference enabled before a subscription is stored tells somebody push
    // is on with nowhere for it to arrive.
    const browser = installBrowserPush();
    const calls = api();
    renderApp({ path: "/settings/notifications" });

    const enable = await screen.findByRole("button", { name: /enable push/i });
    // Nothing has been asked before the click — §7. A prompt on load is the
    // most reliable way to have a permission denied permanently.
    expect(browser.requested).toBe(0);

    await userEvent.click(enable);

    await waitFor(() => expect(calls.patched).toHaveLength(1));
    expect(browser.requested).toBe(1);
    // The three fields the API accepts, and **only** those three: a user id
    // here would be refused by the backend, and its absence is what makes
    // that impossible rather than merely checked.
    expect(Object.keys(calls.registered[0] as object).sort()).toEqual([
      "auth",
      "endpoint",
      "p256dh",
    ]);
    expect(calls.patched[0]).toEqual({
      changes: [{ category: "tournament", channel: "push", enabled: true }],
    });
    // The subscription was stored before the preference moved.
    expect(calls.registered).toHaveLength(1);
  });

  it("subscribes with userVisibleOnly, which every browser requires", async () => {
    // A subscription that could deliver silently is one a page could use to
    // track somebody, so the platform refuses to create one. Omitting this
    // makes `subscribe()` throw — in a real browser, not in this fake, which
    // is exactly why it is asserted here.
    const browser = installBrowserPush();
    api();
    renderApp({ path: "/settings/notifications" });

    await userEvent.click(await screen.findByRole("button", { name: /enable push/i }));

    await waitFor(() => expect(browser.subscribeCalls).toHaveLength(1));
    expect(browser.subscribeCalls[0]).toMatchObject({ userVisibleOnly: true });
  });
});

describe("signing out", () => {
  it("completes on a browser with no service worker registration", async () => {
    // **A regression test for a hang this phase introduced.**
    //
    // `navigator.serviceWorker.ready` resolves when a worker becomes active
    // and **never settles** when none is registered — no rejection, no
    // timeout. The release registered on `onSessionEnding` awaited it, so on
    // any browser without a registration — a first visit before the worker
    // installs, service workers disabled, any context it was never
    // registered in — pressing "sign out" left a spinner forever and the
    // session never ended.
    //
    // It surfaced in the E2E suite as an unrelated spec failing on a
    // protected route that did not bounce to `/login`, which is exactly how
    // a hang presents: as somebody else's assertion.
    installBrowserPush({ permission: "granted" });
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: {
        // The shape a browser with no registration actually has.
        ready: new Promise(() => {}),
        getRegistration: () => Promise.resolve(undefined),
      },
    });
    api();
    renderApp({ path: "/settings/notifications" });

    await userEvent.click(await screen.findByRole("button", { name: /sign out/i }));

    // The header offers a way back in, which it does only once the session
    // has actually been cleared. Before the fix this never arrived.
    // A string `name` matches the **whole** accessible name, so this cannot
    // also match the verification page's "Go to sign in".
    expect(await screen.findByRole("link", { name: "Sign in" })).toBeVisible();
  });
});

describe("disabling", () => {
  it("unsubscribes this browser and removes the record, not just the preference", async () => {
    // **§22's semantics, decided and asserted.** Turning push off here means
    // this device stops receiving push — not that a live capability is left
    // on a browser somebody just asked to stop notifying them.
    const browser = installBrowserPush({ permission: "granted", subscribed: true });
    const calls = api({ deviceCount: 1, pushEnabled: true });
    renderApp({ path: "/settings/notifications" });

    await userEvent.click(await screen.findByRole("button", { name: /turn off here/i }));

    await waitFor(() => expect(calls.removed).toHaveLength(1));
    expect(browser.unsubscribed).toBe(1);
    expect(calls.removed[0]).toEqual({ endpoint: "https://push.example.com/wpush/abc123" });
    expect(calls.patched[0]).toEqual({
      changes: [{ category: "tournament", channel: "push", enabled: false }],
    });
  });
});
