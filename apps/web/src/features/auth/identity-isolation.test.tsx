import { screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AuthBroadcast, AuthChannel } from "@/features/auth/model/auth-channel";
import { exclusively } from "@/features/notification-push/model/exclusive";
import { anonymousId } from "@/shared/analytics";
import { api } from "@/shared/api";
import { httpClient } from "@/shared/api/client";
import { env } from "@/shared/config/env";
import { RealtimeClient } from "@/shared/realtime";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * One account's data never reaches another — A64-031.E5.
 *
 * The refresh cookie is shared by every tab, so a sign-in in another tab can
 * make this tab's next refresh answer for a different account with no
 * sign-out here. Every test drives that through the real app: the real
 * interceptors, the real session provider, the real query client, and a
 * real `RealtimeClient` over a stubbed `WebSocket`. The server is MSW, and
 * it tells accounts apart by the bearer token each request carries.
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

/** Whose session the shared cookie currently holds — the "other tab" moves it. */
let cookie: typeof ALICE | null = ALICE;
let issued = 0;
const expired = new Set<string>();
const tickets: string[] = [];
const sockets: string[] = [];

/** The account a bearer token belongs to, or `null` if it is not live. */
function ownerOf(request: Request): string | null {
  const token = request.headers.get("Authorization")?.replace("Bearer ", "") ?? "";
  if (expired.has(token)) return null;
  const match = /^token-(.+)-\d+$/.exec(token);
  return match?.[1] ?? null;
}

function expiredToken() {
  return HttpResponse.json(
    { code: "expired_token", message: "x", request_id: null, correlation_id: null },
    { status: 401 },
  );
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  httpClient.interceptors.request.clear();
  httpClient.interceptors.response.clear();
  cookie = ALICE;
  issued = 0;
  expired.clear();
  tickets.length = 0;
  sockets.length = 0;

  vi.stubGlobal(
    "WebSocket",
    class {
      static readonly OPEN = 1;
      static readonly CONNECTING = 0;
      readyState = 0;
      onmessage: unknown = null;
      onclose: unknown = null;
      onerror: unknown = null;
      constructor(readonly requestedUrl: string) {
        sockets.push(new URL(requestedUrl).searchParams.get("ticket") ?? "");
      }
      close() {}
      send() {}
    },
  );

  mswServer.use(
    http.post(url("/auth/browser/refresh"), () => {
      if (cookie === null) {
        return HttpResponse.json(
          { code: "invalid_session", message: "x", request_id: null, correlation_id: null },
          { status: 401 },
        );
      }
      issued += 1;
      return HttpResponse.json(
        envelope({ access_token: `token-${cookie.id}-${issued}`, user: cookie }),
      );
    }),
    // A ticket is minted for whoever the bearer token names, and carries it.
    http.post(url("/auth/ws-ticket"), ({ request }) => {
      const owner = ownerOf(request);
      if (owner === null) return expiredToken();
      tickets.push(owner);
      return HttpResponse.json(
        envelope({ ticket: `ticket-for-${owner}`, expires_at: "2026-10-22T10:00:30Z" }),
        { status: 201 },
      );
    }),
    // Private data: answers with the name of the account that asked.
    http.get(url("/private"), ({ request }) => {
      const owner = ownerOf(request);
      return owner === null ? expiredToken() : HttpResponse.json(envelope({ owner }));
    }),
  );
});

/** Signed in as Alice, socket open, one private and one public query cached. */
async function aliceWithCache() {
  const realtime = new RealtimeClient();
  const stop = vi.spyOn(realtime, "stop");
  const rendered = renderApp({ path: "/", realtimeClient: realtime });
  await screen.findByText("Alice");
  await vi.waitFor(() => expect(sockets).toEqual([`ticket-for-${ALICE.id}`]));

  await rendered.queryClient.fetchQuery({
    queryKey: ["private", "inbox"],
    queryFn: () => api.get<{ owner: string }>("/private"),
  });
  rendered.queryClient.setQueryData(["reference", "time-controls"], [{ id: "blitz" }]);
  return { ...rendered, stop };
}

/** Moves the shared cookie to Bob and makes Alice's token expire. */
async function anotherTabSignsInAsBob() {
  cookie = BOB;
  expired.add(`token-${ALICE.id}-1`);
  // Any request now 401s, refreshes, and the refresh answers for Bob.
  return api.get("/private");
}

describe("an ordinary refresh, same account", () => {
  it("keeps the cache and the socket", async () => {
    const { queryClient, stop } = await aliceWithCache();
    expired.add(`token-${ALICE.id}-1`);

    const answer = await api.get<{ owner: string }>("/private");

    expect(answer.owner).toBe(ALICE.id);
    expect(queryClient.getQueryData(["private", "inbox"])).toEqual({ owner: ALICE.id });
    expect(stop).not.toHaveBeenCalled();
    expect(sockets).toEqual([`ticket-for-${ALICE.id}`]);
  });
});

describe("a refresh that answers for another account", () => {
  it("drops the first account's cached data and keeps viewer-independent data", async () => {
    const { queryClient } = await aliceWithCache();

    await expect(anotherTabSignsInAsBob()).rejects.toMatchObject({ kind: "canceled" });

    expect(queryClient.getQueryData(["private", "inbox"])).toBeUndefined();
    expect(queryClient.getQueryData(["reference", "time-controls"])).toEqual([{ id: "blitz" }]);
    const header = await screen.findByRole("banner");
    expect(await within(header).findByText("Bob")).toBeVisible();
    expect(within(header).queryByText("Alice")).not.toBeInTheDocument();
  });

  it("never replays the first account's request as the second", async () => {
    await aliceWithCache();
    const asks: (string | null)[] = [];
    mswServer.use(
      http.post(url("/private/action"), ({ request }) => {
        asks.push(ownerOf(request));
        return ownerOf(request) === null
          ? expiredToken()
          : HttpResponse.json(envelope({ done: true }));
      }),
    );
    cookie = BOB;
    expired.add(`token-${ALICE.id}-1`);

    await expect(api.post("/private/action")).rejects.toMatchObject({ kind: "canceled" });

    // Sent once with Alice's expired token, and never again as Bob.
    expect(asks).toEqual([null]);
  });

  it("closes the first account's socket and opens one minted for the second", async () => {
    const { stop } = await aliceWithCache();

    await anotherTabSignsInAsBob().catch(() => undefined);

    expect(stop).toHaveBeenCalled();
    await vi.waitFor(() =>
      expect(sockets).toEqual([`ticket-for-${ALICE.id}`, `ticket-for-${BOB.id}`]),
    );
    expect(tickets).toEqual([ALICE.id, BOB.id]);
  });
});

describe("the analytics identity", () => {
  it("is replaced when another account takes the tab, and kept by a refresh", async () => {
    // A new visitor id for whoever uses this browser next, as on sign-out:
    // keeping it would join the second account's visit to the first's.
    await aliceWithCache();
    const before = anonymousId();
    expired.add(`token-${ALICE.id}-1`);
    await api.get("/private");
    expect(anonymousId()).toBe(before);

    cookie = BOB;
    expired.add(`token-${ALICE.id}-2`);
    await api.get("/private").catch(() => undefined);

    await vi.waitFor(() => expect(anonymousId()).not.toBe(before));
  });
});

describe("a response still in flight when the account changes", () => {
  function gated(path: string) {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    mswServer.use(
      http.get(url(path), async ({ request }) => {
        const owner = ownerOf(request);
        await gate;
        return owner === null ? expiredToken() : HttpResponse.json(envelope({ owner }));
      }),
    );
    return release;
  }

  it("does not land in the cache", async () => {
    const { queryClient } = await aliceWithCache();
    const release = gated("/private/slow");
    const pending = queryClient.fetchQuery({
      queryKey: ["private", "slow"],
      queryFn: () => api.get<{ owner: string }>("/private/slow"),
    });

    await anotherTabSignsInAsBob().catch(() => undefined);
    release();

    await expect(pending).rejects.toBeDefined();
    expect(queryClient.getQueryData(["private", "slow"])).toBeUndefined();
  });

  it("is refused even outside the query cache", async () => {
    await aliceWithCache();
    const release = gated("/private/direct");
    const pending = api.get<{ owner: string }>("/private/direct");

    await anotherTabSignsInAsBob().catch(() => undefined);
    release();

    // Alice's answer reaches no caller once the tab is Bob's.
    await expect(pending).rejects.toMatchObject({ kind: "canceled" });
  });
});

describe("leaving an account", () => {
  it("drops its data when the session expires", async () => {
    const { queryClient, stop } = await aliceWithCache();
    cookie = null;
    expired.add(`token-${ALICE.id}-1`);

    await expect(api.get("/private")).rejects.toBeDefined();

    expect(queryClient.getQueryData(["private", "inbox"])).toBeUndefined();
    expect(stop).toHaveBeenCalled();
  });

  it("drops its data when another tab signs out", async () => {
    let deliver: (message: AuthBroadcast) => void = () => undefined;
    const channel: AuthChannel = {
      post: () => undefined,
      subscribe: (handler) => {
        deliver = handler;
        return () => undefined;
      },
      close: () => undefined,
    };
    const realtime = new RealtimeClient();
    const { queryClient } = renderApp({ path: "/", channel, realtimeClient: realtime });
    await screen.findByText("Alice");
    await queryClient.fetchQuery({
      queryKey: ["private", "inbox"],
      queryFn: () => api.get<{ owner: string }>("/private"),
    });

    deliver({ type: "logged_out" });

    await vi.waitFor(() =>
      expect(queryClient.getQueryData(["private", "inbox"])).toBeUndefined(),
    );
  });
});

describe("arriving at an account from none", () => {
  it("drops what was fetched anonymously, and keeps viewer-independent data", async () => {
    cookie = null;
    const { queryClient } = renderApp({ path: "/", realtimeClient: new RealtimeClient() });
    const header = await screen.findByRole("banner");
    await within(header).findByRole("link", { name: /sign in|kirish|войти/i });
    queryClient.setQueryData(["profile", "public", "carol"], { relationship: "none" });
    queryClient.setQueryData(["auth", "providers"], { google: true });

    // Signed in elsewhere: the cookie is now Bob's. The next request that
    // needs an account 401s, the refresh answers for Bob, and this tab
    // becomes his.
    cookie = BOB;
    mswServer.use(http.get(url("/needs-account"), () => expiredToken()));
    await api.get("/needs-account").catch(() => undefined);
    // Looked up again: the signed-out landing header is replaced by the
    // app's, so the element found earlier is no longer in the document.
    expect(await within(await screen.findByRole("banner")).findByText("Bob")).toBeVisible();

    await vi.waitFor(() =>
      expect(queryClient.getQueryData(["profile", "public", "carol"])).toBeUndefined(),
    );
    expect(queryClient.getQueryData(["auth", "providers"])).toEqual({ google: true });
  });
});

describe("this browser's push subscription", () => {
  /**
   * A subscription belongs to the browser profile and was registered to the
   * first account. When the tab becomes another account without a sign-out,
   * it is unsubscribed in the browser — and the server is not asked to
   * remove the row, which the new account does not own and could not.
   */
  const push = { unsubscribed: 0, removals: 0 };

  beforeEach(() => {
    push.unsubscribed = 0;
    push.removals = 0;
    let subscription: { endpoint: string; unsubscribe: () => Promise<boolean> } | null = {
      endpoint: "https://push.example/endpoint-of-this-browser",
      unsubscribe: () => {
        push.unsubscribed += 1;
        subscription = null;
        return Promise.resolve(true);
      },
    };
    const pushManager = { getSubscription: () => Promise.resolve(subscription) };
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: {
        ready: Promise.resolve({ pushManager }),
        getRegistration: () => Promise.resolve({ pushManager }),
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
    mswServer.use(
      http.post(url("/notifications/push/subscriptions/remove"), () => {
        push.removals += 1;
        return new HttpResponse(null, { status: 204 });
      }),
      // Registered to Alice — A64-031.E6 asks on every sign-in.
      http.post(url("/notifications/push/subscriptions/ownership"), ({ request }) => {
        const owner = ownerOf(request);
        if (owner === null) return expiredToken();
        return HttpResponse.json(envelope({ owned: owner === ALICE.id }));
      }),
    );
  });

  afterEach(() => {
    // Only what this block defined — `vi.unstubAllGlobals()` would also
    // remove the `matchMedia` stub the shared setup installs.
    Reflect.deleteProperty(globalThis, "Notification");
    Reflect.deleteProperty(globalThis, "PushManager");
    Reflect.deleteProperty(navigator, "serviceWorker");
  });

  it("is unsubscribed when another account replaces this one", async () => {
    await aliceWithCache();

    await anotherTabSignsInAsBob().catch(() => undefined);

    await vi.waitFor(() => expect(push.unsubscribed).toBe(1));
    expect(push.removals).toBe(0);
  });

  it("is released once, not again by the new account's own check", async () => {
    // A64-031.E6: Bob's sign-in asks about the subscription too. Both go
    // through one lock, so the check finds it already gone instead of
    // racing the release to unsubscribe it a second time.
    await aliceWithCache();

    await anotherTabSignsInAsBob().catch(() => undefined);
    await screen.findByText("Bob");
    await exclusively(() => Promise.resolve());

    expect(push.unsubscribed).toBe(1);
  });

  it("is left alone by an ordinary refresh", async () => {
    await aliceWithCache();
    expired.add(`token-${ALICE.id}-1`);

    await api.get("/private");

    expect(push.unsubscribed).toBe(0);
  });
});

describe("the socket's own connect", () => {
  it("never opens with a ticket from before a stop and start", async () => {
    // Below the session provider: the generation guard in `RealtimeClient`
    // on its own, with no interceptor to cancel the stale ticket request.
    const answers: (() => void)[] = [];
    let served = 0;
    mswServer.use(
      http.post(url("/auth/ws-ticket"), async () => {
        served += 1;
        const ticket = `ticket-${served}`;
        await new Promise<void>((resolve) => answers.push(resolve));
        return HttpResponse.json(envelope({ ticket, expires_at: "2026-10-22T10:00:30Z" }), {
          status: 201,
        });
      }),
    );
    const realtime = new RealtimeClient();

    realtime.start();
    await vi.waitFor(() => expect(answers).toHaveLength(1));
    realtime.stop();
    realtime.start();
    await vi.waitFor(() => expect(answers).toHaveLength(2));

    answers[0]?.(); // the first ticket arrives late
    answers[1]?.();

    await vi.waitFor(() => expect(sockets).toEqual(["ticket-2"]));
    realtime.stop();
  });
});
