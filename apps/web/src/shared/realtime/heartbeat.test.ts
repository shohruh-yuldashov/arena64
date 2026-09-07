import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { httpClient } from "@/shared/api/client";
import { env } from "@/shared/config/env";
import { HEARTBEAT_INTERVAL_MS, RealtimeClient } from "@/shared/realtime";
import { mswServer } from "@/shared/test/msw/server";

/**
 * The heartbeat this client owes the gateway — A64-031.A.
 *
 * `app/gateway/connections.py` reads with
 * `wait_for(receive(), GATEWAY_HEARTBEAT_TIMEOUT_SECONDS)` and closes a
 * connection that has said nothing for 45 seconds. The protocol states the
 * direction at `MessageType.PING`: the **client** drives the heartbeat,
 * because a server that pinged would need one timer per socket.
 *
 * This client sent none. The player it disconnected every 45 seconds was
 * precisely the one waiting for their opponent — who by definition sends
 * nothing — and each of those reconnects was an opportunity to miss the move
 * they were waiting for.
 *
 * These assert the lifecycle rather than the wire: that it runs while
 * connected, stops when the connection ends, and — the one a timer in a
 * reconnecting class actually gets wrong — does not accumulate.
 */

const url = (path: string) => `${env.VITE_API_URL}${path}`;
const envelope = <T>(data: T) => ({ data, meta: { request_id: null, correlation_id: null } });

interface StubSocket {
  onmessage: ((event: { data: string }) => void) | null;
  onclose: ((event: { code: number }) => void) | null;
  sent: string[];
  readyState: number;
}

/** Sockets the client opened, newest last. Nothing is answered automatically. */
function stubWebSocket(): StubSocket[] {
  const sockets: StubSocket[] = [];

  vi.stubGlobal(
    "WebSocket",
    class {
      static readonly OPEN = 1;
      static readonly CONNECTING = 0;
      readyState = 1;
      onmessage: ((event: { data: string }) => void) | null = null;
      onclose: ((event: { code: number }) => void) | null = null;
      onerror: unknown = null;
      readonly sent: string[] = [];

      constructor() {
        sockets.push(this);
      }
      close() {
        this.readyState = 3;
      }
      send(frame: string) {
        this.sent.push(frame);
      }
    },
  );

  return sockets;
}

/** Drives the handshake the gateway performs, so the client reaches `ready`. */
function becomeReady(socket: StubSocket): void {
  socket.onmessage?.({
    data: JSON.stringify({ v: 1, type: "connection.ready", channel: "system", payload: {} }),
  });
}

function pings(socket: StubSocket): string[] {
  return socket.sent.filter((frame) => (JSON.parse(frame) as { type: string }).type === "ping");
}

/** Lets the ticket request settle without advancing the fake clock. */
async function settle(): Promise<void> {
  await vi.waitFor(() => expect(sockets.length).toBeGreaterThan(0));
}

let sockets: StubSocket[];

beforeEach(() => {
  vi.useFakeTimers();
  httpClient.interceptors.request.clear();
  httpClient.interceptors.response.clear();
  mswServer.use(
    http.post(url("/auth/ws-ticket"), () =>
      HttpResponse.json(envelope({ ticket: "t1", expires_at: "2026-09-07T10:00:30Z" }), {
        status: 201,
      }),
    ),
  );
  sockets = stubWebSocket();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("pings while the connection is ready, on the system channel", async () => {
  const client = new RealtimeClient();
  client.start();
  await settle();

  const socket = sockets[0]!;
  becomeReady(socket);
  expect(pings(socket)).toHaveLength(0);

  await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS);
  expect(pings(socket)).toHaveLength(1);

  // The frame the gateway's `_answer_ping` expects: `system`, no correlation.
  // A `request_id` would put one pending entry per tick through the request
  // registry, which exists to correlate decisions rather than liveness.
  const frame = JSON.parse(pings(socket)[0]!) as Record<string, unknown>;
  expect(frame.type).toBe("ping");
  expect(frame.channel).toBe("system");
  expect(frame.request_id).toBeUndefined();

  await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS * 2);
  expect(pings(socket)).toHaveLength(3);

  client.stop();
});

it("keeps every tick inside the server's deadline", () => {
  // The number itself is the contract: `GATEWAY_HEARTBEAT_TIMEOUT_SECONDS`
  // is 45 s, and a heartbeat that only just fits leaves no room for one
  // dropped frame. Two ticks inside the window is what makes a single miss
  // survivable rather than fatal.
  expect(HEARTBEAT_INTERVAL_MS * 2).toBeLessThan(45_000);
});

it("clears the timer when the session ends, not merely the writes", async () => {
  const client = new RealtimeClient();
  client.start();
  await settle();

  const socket = sockets[0]!;
  becomeReady(socket);
  await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS);
  expect(pings(socket)).toHaveLength(1);
  expect(vi.getTimerCount()).toBeGreaterThan(0);

  client.stop();

  // **The timer count, not the ping count.** `send` already refuses to write
  // to a socket that is not `ready`, so an interval left running after
  // sign-out sends nothing and looks identical to one that was cleared —
  // while still waking the tab every twenty seconds for the life of the
  // page. Asserting on the writes alone let exactly that through.
  expect(vi.getTimerCount()).toBe(0);

  await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS * 3);
  expect(pings(socket)).toHaveLength(1);
});

it("does not accumulate a second heartbeat across a reconnect", async () => {
  const client = new RealtimeClient();
  client.start();
  await settle();

  const first = sockets[0]!;
  becomeReady(first);
  await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS);
  expect(pings(first)).toHaveLength(1);

  // The gateway drops the connection; the client backs off and returns.
  first.onclose?.({ code: 1006 });
  await vi.advanceTimersByTimeAsync(1_000);
  await vi.waitFor(() => expect(sockets.length).toBe(2));

  const second = sockets[1]!;
  becomeReady(second);

  // One interval, one ping — on the new socket, and none on the old. An
  // interval left running by the previous connection would show up here as
  // two, and would keep compounding with every reconnect.
  await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS);
  expect(pings(second)).toHaveLength(1);
  expect(pings(first)).toHaveLength(1);

  client.stop();
});
