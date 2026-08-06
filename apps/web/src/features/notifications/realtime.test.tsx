import { screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";

import { httpClient } from "@/shared/api/client";
import { env } from "@/shared/config/env";
import { RealtimeClient } from "@/shared/realtime";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * Notifications pushed over the one socket — A64-021.2 §4, §5, §6, §10.
 *
 * Driven through the **real** app, the **real** `RealtimeClient` and the
 * **real** `parseFrame`: a frame is delivered by calling the stubbed
 * socket's `onmessage`, exactly as the browser would. That is deliberate —
 * `parseFrame` is where `notification.created` and the `notifications`
 * channel were added, and a test that handed a pre-parsed object to the
 * hook would not have noticed if either were missing from its allowlist.
 *
 * It also proves the subscription is **mounted**. `useNotificationPush` is
 * called by `AppShell`, so every assertion below reaches it through the
 * real route tree rather than by rendering the hook in isolation — the
 * "implemented and reachable from nothing" failure this codebase keeps
 * finding.
 */

const url = (path: string) => `${env.VITE_API_URL}${path}`;
const REFRESH = url("/auth/browser/refresh");
const TICKET = url("/auth/ws-ticket");

const envelope = <T,>(data: T) => ({ data, meta: { request_id: null, correlation_id: null } });

const VIEWER = {
  id: "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
  username: "viewer",
  display_name: "Viewer",
  email: "viewer@example.com",
  is_active: true,
  is_verified: true,
};

const NOTIFICATION_ID = "019fe500-0000-7000-8000-000000000001";

/** Every socket the app opened, so a test can push a frame into one. */
interface StubSocket {
  onmessage: ((event: { data: string }) => void) | null;
}

function stubWebSocket(): StubSocket[] {
  const sockets: StubSocket[] = [];
  vi.stubGlobal(
    "WebSocket",
    class {
      static readonly OPEN = 1;
      static readonly CONNECTING = 0;
      readyState = 0;
      onmessage: ((event: { data: string }) => void) | null = null;
      onclose: unknown = null;
      onerror: unknown = null;
      onopen: unknown = null;
      constructor() {
        sockets.push(this);
      }
      close() {}
      send() {}
    },
  );
  return sockets;
}

/** One `notification.created` frame, as the gateway encodes it. */
function push(socket: StubSocket, notificationId = NOTIFICATION_ID): void {
  socket.onmessage?.({
    data: JSON.stringify({
      v: 1,
      type: "notification.created",
      request_id: null,
      channel: "notifications",
      payload: {
        notification_id: notificationId,
        type: "friend_request_received",
        created_at: "2026-08-06T10:00:00Z",
      },
    }),
  });
}

/** What the badge and the list currently answer, and how often they were asked. */
interface Backend {
  unreadReads: number;
  listReads: number;
  unread: number;
  entries: Record<string, unknown>[];
}

function serve(backend: Backend): void {
  mswServer.use(
    http.post(REFRESH, () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: VIEWER })),
    ),
    http.post(TICKET, () =>
      HttpResponse.json(envelope({ ticket: "t1", expires_at: "2026-08-06T10:00:30Z" }), {
        status: 201,
      }),
    ),
    http.get(url("/notifications/unread-count"), () => {
      backend.unreadReads += 1;
      return HttpResponse.json(envelope({ unread_count: backend.unread }));
    }),
    http.get(url("/notifications"), () => {
      backend.listReads += 1;
      return HttpResponse.json(envelope({ entries: backend.entries, next_cursor: null }));
    }),
  );
}

function notification(overrides: Record<string, unknown> = {}) {
  return {
    id: NOTIFICATION_ID,
    type: "friend_request_received",
    category: "social",
    actor: {
      player_id: "019fb9ea-0a0c-7cec-9c5f-402727c31b01",
      username: "rival",
      display_name: "Rival",
      thumbnail_url: null,
    },
    target: { type: "friend_requests", ref: null },
    created_at: "2026-08-06T10:00:00Z",
    read_at: null,
    is_read: false,
    ...overrides,
  };
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  httpClient.interceptors.request.clear();
  httpClient.interceptors.response.clear();
});

it("updates the badge from a frame, and collapses duplicates into one refetch", async () => {
  const backend: Backend = { unreadReads: 0, listReads: 0, unread: 0, entries: [] };
  serve(backend);
  const sockets = stubWebSocket();

  renderApp({ path: "/notifications", realtimeClient: new RealtimeClient() });

  // Caught up, and the socket is open — the state a player sits in.
  expect(await screen.findByRole("link", { name: "Notifications" })).toBeVisible();
  await waitFor(() => expect(sockets.length).toBeGreaterThan(0));
  const before = { unread: backend.unreadReads, list: backend.listReads };

  // Something happened while they were looking at the page.
  backend.unread = 1;
  backend.entries = [notification()];
  push(sockets[0]!);

  // §10.7: the badge updates with **no refresh and no focus event**.
  expect(await screen.findByRole("link", { name: "Notifications — 1 unread" })).toBeVisible();
  expect(await screen.findByText("Rival sent you a friend request")).toBeVisible();

  // Exactly one refetch of each — §4's "invalidate these two, nothing else".
  expect(backend.unreadReads).toBe(before.unread + 1);
  expect(backend.listReads).toBe(before.list + 1);

  // §4, §10.4: the same notification pushed again is not news. A reconnect
  // replaying its backlog must not cost a request per replayed frame.
  const afterFirst = { unread: backend.unreadReads, list: backend.listReads };
  push(sockets[0]!);
  push(sockets[0]!);
  push(sockets[0]!);

  await new Promise((resolve) => setTimeout(resolve, 20));
  expect(backend.unreadReads).toBe(afterFirst.unread);
  expect(backend.listReads).toBe(afterFirst.list);
});

it("lets HTTP decide, so a late frame cannot reopen a read notification", async () => {
  // §5, §10.5. The frame is a wake-up signal and nothing more: the server
  // says this notification is already read, and the badge must believe the
  // server rather than the frame that woke it.
  //
  // This is the failure the rule exists for — a player reads a notification
  // on their phone, and the push for it reaches their laptop a moment
  // later. A handler that incremented a count would reopen a badge for
  // something already dealt with.
  const backend: Backend = {
    unreadReads: 0,
    listReads: 0,
    unread: 0,
    entries: [notification({ is_read: true, read_at: "2026-08-06T10:05:00Z" })],
  };
  serve(backend);
  const sockets = stubWebSocket();

  renderApp({ path: "/notifications", realtimeClient: new RealtimeClient() });

  expect(await screen.findByRole("link", { name: "Notifications" })).toBeVisible();
  await waitFor(() => expect(sockets.length).toBeGreaterThan(0));
  const before = backend.unreadReads;

  push(sockets[0]!);

  // The invalidation happened — the server was asked again.
  await waitFor(() => expect(backend.unreadReads).toBe(before + 1));

  // And the answer stood: no badge, because nothing is unread.
  expect(screen.getByRole("link", { name: "Notifications" })).toBeVisible();
  expect(
    screen.queryByRole("link", { name: /Notifications — \d+ unread/ }),
  ).not.toBeInTheDocument();
});

it("works with no socket at all, so realtime only removes latency", async () => {
  // §6, §10.6. The fallback is not a code path — it is the absence of one.
  // Nothing was taken away in this phase: the badge and the list are the
  // same queries A64-021.1 shipped, and a build whose socket never connects
  // is that product exactly.
  //
  // Asserted by rendering with a `WebSocket` constructor that throws, which
  // is the harshest form of "unavailable" a browser offers.
  const backend: Backend = {
    unreadReads: 0,
    listReads: 0,
    unread: 2,
    entries: [notification()],
  };
  serve(backend);
  vi.stubGlobal(
    "WebSocket",
    class {
      static readonly OPEN = 1;
      static readonly CONNECTING = 0;
      constructor() {
        throw new Error("no socket here");
      }
    },
  );

  renderApp({ path: "/notifications", realtimeClient: new RealtimeClient() });

  // The badge and the list both answered, from HTTP alone.
  expect(await screen.findByRole("link", { name: "Notifications — 2 unread" })).toBeVisible();
  expect(await screen.findByText("Rival sent you a friend request")).toBeVisible();
  expect(backend.unreadReads).toBeGreaterThan(0);
  expect(backend.listReads).toBeGreaterThan(0);
});
