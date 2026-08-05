import { screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";

import { httpClient } from "@/shared/api/client";
import { env } from "@/shared/config/env";
import { deliveryMode } from "@/shared/realtime";
import { RealtimeClient } from "@/shared/realtime";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * Match offers, pushed and reconciled — A64-020.5D §23.
 *
 * Driven through the **real** app: real router at `/play`, real providers,
 * real `RealtimeClient`, real lobby queries. Only `WebSocket` is stubbed,
 * so what is proven is that a frame arriving on the shared socket reaches
 * the lobby's authoritative read — not that a mock was called.
 *
 * §23's items 4, 5 and 7 in two tests, because they are three properties of
 * one flow: a push causes exactly one reconciliation, duplicates cause
 * none, and the HTTP answer wins over the payload.
 */

const url = (path: string) => `${env.VITE_API_URL}${path}`;
const envelope = <T,>(data: T) => ({ data, meta: { request_id: null, correlation_id: null } });
const problem = (status: number, code: string) =>
  HttpResponse.json({ type: "about:blank", title: code, status, code }, { status });

const VIEWER = {
  id: "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
  username: "viewer",
  display_name: "Viewer",
  email: "viewer@example.com",
  is_active: true,
  is_verified: true,
};

const MATCH = "019fd0bb-2222-7000-8000-000000000002";

const CATALOGUE = [
  {
    id: "blitz_3_2",
    label: "3+2",
    base_time_ms: 180_000,
    increment_ms: 2_000,
    speed_class: "blitz",
    display_order: 1,
  },
];

const TICKET = {
  ticket_id: "019fd0aa-1111-7000-8000-000000000001",
  variant: "russian_8x8",
  queue_type: "casual",
  region: "global",
  status: "waiting",
  time_control_id: "blitz_3_2",
  base_time_ms: 180_000,
  increment_ms: 2_000,
  speed_class: "blitz",
  rating_snapshot: 1500,
  entered_at: "2026-08-05T10:00:00Z",
  expires_at: "2026-08-05T10:10:00Z",
  waiting: 2,
};

const OFFER = {
  match_id: MATCH,
  status: "pending_acceptance",
  your_side: "light",
  opponent: {
    player_id: "019fd0cc-3333-7000-8000-000000000003",
    username: "rival",
    display_name: "Rival",
  },
  variant: "russian_8x8",
  rated: false,
  base_time_ms: 180_000,
  increment_ms: 2_000,
  speed_class: "blitz",
  acceptance_deadline: "2099-08-05T10:00:30Z",
  you_accepted: false,
  opponent_accepted: false,
  created_at: "2026-08-05T10:00:00Z",
};

/** The pushed frame, in the gateway's own shape. */
function pushedOffer(matchId = MATCH) {
  return JSON.stringify({
    v: 1,
    type: "matchmaking.match.offered",
    channel: "matchmaking",
    payload: {
      match_id: matchId,
      status: "pending_acceptance",
      your_side: "light",
      opponent: null,
      variant: "russian_8x8",
      rated: false,
      time_control: { initial_ms: 180_000, increment_ms: 2_000 },
      speed_class: "blitz",
      acceptance_deadline: "2099-08-05T10:00:30Z",
      you_accepted: false,
      opponent_accepted: false,
      created_at: "2026-08-05T10:00:00Z",
    },
  });
}

/** A socket whose frames a test drives. Returns the injector. */
function stubSocket(): { push: (frame: string) => void } {
  let onmessage: ((event: { data: string }) => void) | null = null;
  vi.stubGlobal(
    "WebSocket",
    class {
      static readonly OPEN = 1;
      static readonly CONNECTING = 0;
      readyState = 1;
      onclose: unknown = null;
      onerror: unknown = null;
      set onmessage(handler: (event: { data: string }) => void) {
        onmessage = handler;
      }
      constructor(readonly requestedUrl: string) {
        queueMicrotask(() =>
          onmessage?.({
            data: JSON.stringify({
              v: 1,
              type: "connection.ready",
              channel: "system",
              payload: {},
            }),
          }),
        );
      }
      close() {}
      send() {}
    },
  );
  return { push: (frame) => onmessage?.({ data: frame }) };
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  httpClient.interceptors.request.clear();
  httpClient.interceptors.response.clear();
  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: VIEWER })),
    ),
    http.post(url("/auth/ws-ticket"), () =>
      HttpResponse.json(envelope({ ticket: "t1", expires_at: "2099-08-05T10:00:30Z" }), {
        status: 201,
      }),
    ),
    http.get(url("/time-controls"), () => HttpResponse.json(envelope(CATALOGUE))),
  );
});

it("reconciles once on a push and ignores the duplicates", async () => {
  // §23.4 and §23.5, and the second is why the first is worth asserting by
  // *count*: a reconnect can replay several frames for one match, and three
  // pushes must not be three reads.
  let pendingReads = 0;
  let paired = false;

  mswServer.use(
    http.get(url("/matchmaking/queue/me"), () =>
      paired ? problem(404, "not_found") : HttpResponse.json(envelope(TICKET)),
    ),
    http.get(url("/matchmaking/matches/pending"), () => {
      pendingReads += 1;
      // **The server decides**, not the payload: the offer only exists
      // once the pairing has actually happened here.
      return paired ? HttpResponse.json(envelope(OFFER)) : problem(404, "not_found");
    }),
  );

  const socket = stubSocket();
  renderApp({ path: "/play", realtimeClient: new RealtimeClient() });

  // Queued, and the socket is ready — so the lobby is on the slow safety
  // interval rather than polling every two seconds.
  await screen.findByText(/searching for an opponent/i, undefined, { timeout: 5000 });
  const before = pendingReads;

  paired = true;
  socket.push(pushedOffer());

  // One push, one reconciliation, and the dialog comes from the **HTTP**
  // answer rather than from the frame — the frame carried no opponent and
  // the rendered card names one.
  const dialog = await screen.findByRole("alertdialog", undefined, { timeout: 5000 });
  expect(dialog).toHaveTextContent(/rival/i);

  const afterFirst = pendingReads;
  expect(afterFirst).toBeGreaterThan(before);

  // Three more of the same frame. Deduplicated by match id, so no further
  // reads at all — asserted after a settle window so this cannot pass by
  // being early.
  socket.push(pushedOffer());
  socket.push(pushedOffer());
  socket.push(pushedOffer());
  await new Promise((resolve) => setTimeout(resolve, 50));
  expect(pendingReads).toBe(afterFirst);

  // And exactly one dialog, not four.
  expect(screen.getAllByRole("alertdialog")).toHaveLength(1);
});

it("lets the authoritative read veto a push for a match that is already over", async () => {
  // §23.7 and §3 — "a delayed push must not resurrect a declined or
  // completed match".
  //
  // The frame says `pending_acceptance`; the server says the match is gone.
  // A client that rendered the payload would open an acceptance dialog for
  // a game somebody already declined, and its Accept button would fail.
  let pendingReads = 0;

  mswServer.use(
    http.get(url("/matchmaking/queue/me"), () => HttpResponse.json(envelope(TICKET))),
    http.get(url("/matchmaking/matches/pending"), () => {
      pendingReads += 1;
      return problem(404, "not_found");
    }),
  );

  const socket = stubSocket();
  renderApp({ path: "/play", realtimeClient: new RealtimeClient() });
  await screen.findByText(/searching for an opponent/i, undefined, { timeout: 5000 });

  socket.push(pushedOffer());

  // The read happened — the push was not ignored — and no dialog appeared.
  await waitFor(() => expect(pendingReads).toBeGreaterThan(0));
  await new Promise((resolve) => setTimeout(resolve, 50));
  expect(screen.queryByRole("alertdialog")).toBeNull();
  // Still queued, which is what the server says.
  expect(screen.getByText(/searching for an opponent/i)).toBeInTheDocument();
});

it("names the delivery mode from the connection status alone", () => {
  // §17. A pure mapping, asserted directly because it is what decides
  // whether a queued player is told anything at all — and the rule that
  // matters is that **only** `ready` counts as realtime. A status that
  // resolved to `realtime` while the socket was reconnecting would leave a
  // player on the slow interval with no push coming.
  expect(deliveryMode("ready")).toBe("realtime");
  expect(deliveryMode("offline")).toBe("offline");
  for (const pursuing of ["ticketing", "connecting", "reconnecting"] as const) {
    expect(deliveryMode(pursuing)).toBe("reconnecting");
  }
  for (const resting of ["idle", "closed", "fatal"] as const) {
    expect(deliveryMode(resting)).toBe("fallback_polling");
  }
});
