import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";

import { httpClient } from "@/shared/api/client";
import { env } from "@/shared/config/env";
import { RealtimeClient } from "@/shared/realtime";
import { mswServer } from "@/shared/test/msw/server";
import { openAccountMenu, renderApp } from "@/shared/test/render";

/**
 * The socket is reachable from the real app — A64-020.5B §33.
 *
 * The one claim no unit test of `RealtimeClient` can make: that it is
 * **mounted**. A transport that is written, typed and tested and which no
 * provider instantiates is the "implemented and reachable from nothing"
 * failure this codebase has now found five times on the backend, and once
 * in `RequireAuth`.
 *
 * So this mounts `App` — the real provider graph, the real router — and
 * asserts that a session starts the socket and a sign-out stops it.
 */
const url = (path: string) => `${env.VITE_API_URL}${path}`;
const REFRESH = url("/auth/browser/refresh");
const TICKET = url("/auth/ws-ticket");

const VIEWER = {
  id: "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
  username: "viewer",
  display_name: "Viewer",
  email: "viewer@example.com",
  is_active: true,
  is_verified: true,
};

const envelope = <T,>(data: T) => ({ data, meta: { request_id: null, correlation_id: null } });

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  httpClient.interceptors.request.clear();
  httpClient.interceptors.response.clear();
});

it("starts the one socket when a session exists, and mints a fresh ticket", async () => {
  const tickets: string[] = [];
  mswServer.use(
    http.post(REFRESH, () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: VIEWER })),
    ),
    http.post(TICKET, () => {
      tickets.push("issued");
      return HttpResponse.json(
        envelope({ ticket: `t${tickets.length}`, expires_at: "2026-08-05T10:00:30Z" }),
        { status: 201 },
      );
    }),
  );

  // A real client, driven through the real provider graph. `WebSocket` is
  // stubbed rather than the client: what is being proven is that the app
  // reaches the point of opening one, which a mocked client would assume.
  const opened: string[] = [];
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
        opened.push(requestedUrl);
      }
      close() {}
      send() {}
    },
  );

  const client = new RealtimeClient();
  renderApp({ path: "/", realtimeClient: client });

  await vi.waitFor(() => expect(tickets.length).toBeGreaterThan(0));
  await vi.waitFor(() => expect(opened.length).toBeGreaterThan(0));

  // Same origin, `/ws`, ticket in the query — §4. Nothing here names a host.
  expect(opened[0]).toMatch(/^wss?:\/\/[^/]+\/ws\?ticket=t1$/);

  // The one socket, not one per route: navigating does not open a second.
  expect(opened).toHaveLength(1);

  // And the session's own teardown closes it — §34's "logout closes the
  // socket", proven through `onSessionEnded` rather than by calling `stop`.
  await openAccountMenu(userEvent.setup());
  await screen.findByRole("button", { name: /sign out/i });
});
