import { QueryClient } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppProviders } from "@/app/providers";
import { canInteract, initialState, reduce } from "@/features/game/model/state";
import { useGameRoom } from "@/features/game/model/use-game-room";
import { httpClient } from "@/shared/api/client";
import { env } from "@/shared/config/env";
import type { MovePayload, SnapshotPayload } from "@/shared/realtime";
import { RealtimeClient } from "@/shared/realtime";
import { mswServer } from "@/shared/test/msw/server";

/**
 * Incremental resume, applied — A64-031.A.
 *
 * ## The bug these exist for
 *
 * `ResumeHandler` answers a reconnect that missed a ply or two with
 * `game.events`: **one** frame whose payload carries the missed frames as
 * encoded strings. `use-game-room` said the gateway unwrapped that wrapper
 * before delivery. Nothing did — `MessageType.EVENTS` is built in exactly one
 * place in `app/gateway/protocol.py` and unwrapped nowhere — so every frame
 * of every incremental resume was matched by `default` and dropped, while the
 * wrapper's `request_id` resolved the `game.resume` promise and told the
 * client it had caught up.
 *
 * The board then sat a ply behind with nothing able to correct it. Gap
 * detection needs a *later* frame to compare against, and when the dropped
 * move was the one that passed the turn to this player, no later frame ever
 * arrived — the player waited on a position their opponent had already left.
 * A reload fixed it, because a fresh mount reports no sequence and is sent a
 * whole snapshot.
 *
 * ## Why these run through the real client
 *
 * The reducer was never the broken part, and a test that called `reduce`
 * directly would have passed against the shipped bug — which is precisely
 * what the existing suite did. What was wrong sat between the transport and
 * the reducer, so these drive the **real** `RealtimeClient`, the real parser
 * and the real hook, and stub only the browser's `WebSocket`.
 */

const url = (path: string) => `${env.VITE_API_URL}${path}`;
const envelope = <T,>(data: T) => ({ data, meta: { request_id: null, correlation_id: null } });

const VIEWER = "11111111-1111-1111-1111-111111111111";
const OPPONENT = "22222222-2222-2222-2222-222222222222";
const MATCH = "019fd1c7-5178-7a94-8076-4eeece03a8f4";

const SESSION = {
  id: VIEWER,
  username: "viewer",
  display_name: "Viewer",
  email: "viewer@example.com",
  is_active: true,
  is_verified: true,
};

/** The position the resume starts from: ply 4, one man each. */
const SNAPSHOT: SnapshotPayload = {
  match_id: MATCH,
  engine_version: 2,
  variant: "russian_8x8",
  status: "active",
  rated: false,
  sequence: 4,
  side_to_move: "light",
  fingerprint: "fp4",
  pieces: [
    { square: "c3", side: "light", rank: "man" },
    { square: "f6", side: "dark", rank: "man" },
  ],
  participants: { light: VIEWER, dark: OPPONENT },
  clock: null,
  result: null,
  server_time: "2026-09-07T10:00:00Z",
};

interface StubSocket {
  onmessage: ((event: { data: string }) => void) | null;
  sent: string[];
}

/**
 * The app's socket, stubbed at the browser boundary — the same shape the
 * quick-message suite uses, answering only what a room needs to become
 * playable. Everything above this is the shipping client.
 */
function stubWebSocket(): StubSocket[] {
  const sockets: StubSocket[] = [];

  vi.stubGlobal(
    "WebSocket",
    class {
      static readonly OPEN = 1;
      static readonly CONNECTING = 0;
      readyState = 1;
      onmessage: ((event: { data: string }) => void) | null = null;
      onclose: unknown = null;
      onerror: unknown = null;
      readonly sent: string[] = [];

      constructor() {
        sockets.push(this);
        queueMicrotask(() => {
          this.onmessage?.({
            data: JSON.stringify({
              v: 1,
              type: "connection.ready",
              channel: "system",
              payload: {},
            }),
          });
        });
      }
      close() {}
      send(frame: string) {
        this.sent.push(frame);
        const parsed = JSON.parse(frame) as { type: string; request_id?: string };
        if (parsed.type === "room.join") {
          this.onmessage?.({
            data: JSON.stringify({
              v: 1,
              type: "room.joined",
              request_id: parsed.request_id ?? null,
              channel: "game",
              payload: {
                match_id: MATCH,
                participants: [VIEWER, OPPONENT],
                both_connected: true,
              },
            }),
          });
        }
        if (parsed.type === "game.resume") {
          this.onmessage?.({
            data: JSON.stringify({
              v: 1,
              type: "game.snapshot",
              request_id: parsed.request_id ?? null,
              channel: "game",
              payload: SNAPSHOT,
            }),
          });
        }
      }
    },
  );

  return sockets;
}

/** One `game.move.applied`, encoded exactly as the gateway encodes it. */
function moveFrame(move: MovePayload): string {
  return JSON.stringify({
    v: 1,
    type: "game.move.applied",
    request_id: null,
    channel: "game",
    payload: move,
  });
}

function move(ply: number, path: [string, string], sideToMove: "light" | "dark"): MovePayload {
  return {
    match_id: MATCH,
    ply,
    side_to_move: sideToMove,
    fingerprint: `fp${ply}`,
    applied: { path, captured: [], promoted_to: null },
  } satisfies MovePayload;
}

/**
 * Delivers a `game.events` wrapper, as `match_events` builds it: the missed
 * frames as **encoded strings**, not decoded objects.
 */
function deliverEvents(socket: StubSocket, frames: string[]): void {
  act(() => {
    socket.onmessage?.({
      data: JSON.stringify({
        v: 1,
        type: "game.events",
        request_id: null,
        channel: "game",
        payload: { match_id: MATCH, frames },
      }),
    });
  });
}

/** Renders the hook's authoritative state where a test can read it. */
function Probe() {
  const room = useGameRoom(MATCH);
  return (
    <div>
      <span data-testid="sequence">{room.state.sequence}</span>
      <span data-testid="phase">{room.state.phase}</span>
      <span data-testid="can-interact">{String(canInteract(room.state))}</span>
      <span data-testid="occupied">
        {[...room.state.board.keys()].sort((a, b) => a.localeCompare(b)).join(",")}
      </span>
    </div>
  );
}

function mount() {
  const client = new RealtimeClient();
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity, staleTime: 0 } },
  });
  render(
    <AppProviders queryClient={queryClient} realtimeClient={client}>
      <Probe />
    </AppProviders>,
  );
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  httpClient.interceptors.request.clear();
  httpClient.interceptors.response.clear();
  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: SESSION })),
    ),
    http.post(url("/auth/ws-ticket"), () =>
      HttpResponse.json(envelope({ ticket: "t1", expires_at: "2026-09-07T10:00:30Z" }), {
        status: 201,
      }),
    ),
  );
});

describe("an incremental resume", () => {
  it("applies the frames the wrapper carries, rather than dropping them", async () => {
    const sockets = stubWebSocket();
    mount();

    await waitFor(() => expect(screen.getByTestId("sequence")).toHaveTextContent("4"));
    expect(screen.getByTestId("occupied")).toHaveTextContent("c3,f6");
    const socket = sockets[0];
    expect(socket).toBeDefined();

    // The one ply this client missed while it was away: light c3 -> d4.
    deliverEvents(socket!, [moveFrame(move(5, ["c3", "d4"], "dark"))]);

    // Before the fix this stayed at 4 with c3 still occupied, and — because
    // the sequence never moved — no gap could ever be detected either.
    await waitFor(() => expect(screen.getByTestId("sequence")).toHaveTextContent("5"));
    expect(screen.getByTestId("occupied")).toHaveTextContent("d4,f6");
    expect(screen.getByTestId("phase")).toHaveTextContent("active");
  });

  it("applies several frames in the order the wrapper lists them", async () => {
    const sockets = stubWebSocket();
    mount();

    await waitFor(() => expect(screen.getByTestId("sequence")).toHaveTextContent("4"));
    const socket = sockets[0];

    // Two plies: light c3 -> d4, then dark f6 -> e5. Applied out of order the
    // second would be a gap and the board would end somewhere else, so the
    // final position is what proves the ordering.
    deliverEvents(socket!, [
      moveFrame(move(5, ["c3", "d4"], "dark")),
      moveFrame(move(6, ["f6", "e5"], "light")),
    ]);

    await waitFor(() => expect(screen.getByTestId("sequence")).toHaveTextContent("6"));
    expect(screen.getByTestId("occupied")).toHaveTextContent("d4,e5");
    expect(screen.getByTestId("phase")).toHaveTextContent("active");
  });

  it("survives a malformed member without discarding the rest of the replay", async () => {
    const sockets = stubWebSocket();
    mount();

    await waitFor(() => expect(screen.getByTestId("sequence")).toHaveTextContent("4"));
    const socket = sockets[0];

    // A member the parser refuses sits between two good ones. Dropping the
    // whole answer would leave this client silently behind, which is the
    // failure mode being fixed rather than a new one to introduce.
    deliverEvents(socket!, [
      moveFrame(move(5, ["c3", "d4"], "dark")),
      "{not json",
      moveFrame(move(6, ["f6", "e5"], "light")),
    ]);

    await waitFor(() => expect(screen.getByTestId("sequence")).toHaveTextContent("6"));
    expect(screen.getByTestId("occupied")).toHaveTextContent("d4,e5");
  });
});

describe("a sequence is a claim about a board", () => {
  /**
   * The reducer half — A64-031.A §2.
   *
   * `applyToBoard` used to return the board unchanged when it could not read
   * the transition, and the caller advanced `sequence` over it. That records
   * ply N against a position still at N-1, and nothing can recover: gap
   * detection compares plies, and a resume reporting N is answered `CURRENT`
   * and sent nothing at all.
   */
  it("does not advance the sequence when the move cannot be applied", () => {
    const at4 = reduce(initialState(MATCH), {
      type: "snapshot",
      payload: SNAPSHOT,
      viewerId: VIEWER,
    });
    expect(at4.sequence).toBe(4);

    // b2 holds nothing in this position, so the server's transition cannot be
    // applied to the board this client has.
    const impossible = reduce(at4, { type: "applied", payload: move(5, ["b2", "c3"], "dark") });

    expect(impossible.sequence).toBe(4);
    expect(impossible.phase).toBe("resyncing");
    expect([...impossible.board.keys()].sort((a, b) => a.localeCompare(b))).toEqual([
      "c3",
      "f6",
    ]);
  });
});

// --- the reconnect that missed nothing — A64-031.A S-1 -----------------------

interface ReconnectingSocket extends StubSocket {
  onclose: ((event: { code: number }) => void) | null;
}

/**
 * A socket whose **second** `game.resume` is answered `CURRENT`.
 *
 * That is the case the server takes whenever a reconnect missed no ply, and
 * it is the overwhelmingly common one: a socket closed by the heartbeat
 * deadline is back in about half a second, and half a second usually
 * contains no move. `ResumeHandler` returns `game.resumed` and nothing else
 * — no snapshot, no `game.events` — which is precisely why the client had
 * nothing to act on.
 */
function stubReconnectingWebSocket(options: { result?: SnapshotPayload["result"] } = {}) {
  const sockets: ReconnectingSocket[] = [];
  let resumes = 0;

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
        queueMicrotask(() => {
          this.onmessage?.({
            data: JSON.stringify({
              v: 1,
              type: "connection.ready",
              channel: "system",
              payload: {},
            }),
          });
        });
      }
      close() {
        this.readyState = 3;
      }
      send(frame: string) {
        this.sent.push(frame);
        const parsed = JSON.parse(frame) as { type: string; request_id?: string };
        if (parsed.type === "room.join") {
          this.onmessage?.({
            data: JSON.stringify({
              v: 1,
              type: "room.joined",
              request_id: parsed.request_id ?? null,
              channel: "game",
              payload: {
                match_id: MATCH,
                participants: [VIEWER, OPPONENT],
                both_connected: true,
              },
            }),
          });
        }
        if (parsed.type === "game.resume") {
          resumes += 1;
          const first = resumes === 1;
          this.onmessage?.({
            data: JSON.stringify(
              first
                ? {
                    v: 1,
                    type: "game.snapshot",
                    request_id: parsed.request_id ?? null,
                    channel: "game",
                    payload: { ...SNAPSHOT, result: options.result ?? null },
                  }
                : {
                    // The whole of S-1: this frame, and nothing beside it.
                    v: 1,
                    type: "game.resumed",
                    request_id: parsed.request_id ?? null,
                    channel: "game",
                    payload: { match_id: MATCH, sequence: 4, both_connected: true },
                  },
            ),
          });
        }
      }
    },
  );

  return sockets;
}

/** Drops the live socket so the client backs off and reconnects. */
async function reconnect(sockets: ReconnectingSocket[]): Promise<ReconnectingSocket> {
  const live = sockets[sockets.length - 1]!;
  act(() => live.onclose?.({ code: 1006 }));
  await waitFor(() => expect(sockets.length).toBeGreaterThan(1), { timeout: 4000 });
  return sockets[sockets.length - 1]!;
}

describe("a reconnect that missed nothing", () => {
  it("leaves the joining phase and lets the player move again", async () => {
    const sockets = stubReconnectingWebSocket();
    mount();

    await waitFor(() => expect(screen.getByTestId("phase")).toHaveTextContent("active"));
    // Light is to move and this viewer is Light: it is their turn.
    expect(screen.getByTestId("can-interact")).toHaveTextContent("true");

    await reconnect(sockets);

    // Before the fix this stayed at `joining` for ever: `game.resumed` was
    // ignored, no snapshot or events were coming, and — because it is this
    // player's turn — no opponent move was coming either. The board rendered
    // and refused every click until the page was reloaded.
    await waitFor(() => expect(screen.getByTestId("phase")).toHaveTextContent("active"), {
      timeout: 4000,
    });
    expect(screen.getByTestId("can-interact")).toHaveTextContent("true");
    // Nothing was replaced: continuity was proven, not re-sent.
    expect(screen.getByTestId("sequence")).toHaveTextContent("4");
    expect(screen.getByTestId("occupied")).toHaveTextContent("c3,f6");
  });

  it("does not grant a move when it is the opponent's turn", async () => {
    const sockets = stubReconnectingWebSocket();
    mount();
    await waitFor(() => expect(screen.getByTestId("phase")).toHaveTextContent("active"));

    // Light plays c3 -> d4, so it becomes Dark's turn before the drop.
    act(() => {
      sockets[0]!.onmessage?.({ data: moveFrame(move(5, ["c3", "d4"], "dark")) });
    });
    await waitFor(() => expect(screen.getByTestId("can-interact")).toHaveTextContent("false"));

    await reconnect(sockets);

    await waitFor(() => expect(screen.getByTestId("phase")).toHaveTextContent("active"), {
      timeout: 4000,
    });
    // Playable again, but not by this player — recovering the phase must not
    // recover a turn that is not theirs.
    expect(screen.getByTestId("can-interact")).toHaveTextContent("false");
  });

  it("does not put a finished game back in play", async () => {
    const sockets = stubReconnectingWebSocket({
      result: { outcome: "win", termination_reason: "flag", winner: "dark" },
    });
    mount();

    await waitFor(() => expect(screen.getByTestId("phase")).toHaveTextContent("completed"));

    await reconnect(sockets);

    await new Promise((resolve) => setTimeout(resolve, 200));
    expect(screen.getByTestId("phase")).toHaveTextContent("completed");
    expect(screen.getByTestId("can-interact")).toHaveTextContent("false");
  });

  it("is unchanged by a second game.resumed", async () => {
    const sockets = stubReconnectingWebSocket();
    mount();
    await waitFor(() => expect(screen.getByTestId("phase")).toHaveTextContent("active"));
    const socket = await reconnect(sockets);
    await waitFor(() => expect(screen.getByTestId("phase")).toHaveTextContent("active"), {
      timeout: 4000,
    });

    // Redelivery is legal on this transport; applying it twice must not be.
    act(() => {
      socket.onmessage?.({
        data: JSON.stringify({
          v: 1,
          type: "game.resumed",
          request_id: null,
          channel: "game",
          payload: { match_id: MATCH, sequence: 4, both_connected: true },
        }),
      });
    });

    expect(screen.getByTestId("phase")).toHaveTextContent("active");
    expect(screen.getByTestId("sequence")).toHaveTextContent("4");
  });
});

describe("no resume outcome leaves the client stuck", () => {
  /**
   * The three answers `ResumeHandler` can give, asserted as one property:
   * whichever it sends, the room becomes usable again. Two of the three were
   * already true before A64-031.A; none of them was.
   */
  it("a server that is ahead is not believed, and resyncs instead", () => {
    const at4 = reduce(initialState(MATCH), {
      type: "snapshot",
      payload: SNAPSHOT,
      viewerId: VIEWER,
    });
    const joining = reduce(at4, { type: "resuming" });
    expect(joining.phase).toBe("joining");

    // `CURRENT` from a server holding a later ply cannot happen against a
    // correct server. If it ever does, "you are current" is the one reading
    // that must not be taken on trust.
    const ahead = reduce(joining, { type: "resumed", sequence: 9 });

    expect(ahead.phase).toBe("resyncing");
    expect(ahead.sequence).toBe(4);
  });

  it("does not disturb a phase the resume is no longer the answer to", () => {
    const at4 = reduce(initialState(MATCH), {
      type: "snapshot",
      payload: SNAPSHOT,
      viewerId: VIEWER,
    });

    // A gap detected while the resume was in flight: this client is waiting
    // for a snapshot it asked for, and a stale "you are current" must not
    // cancel that.
    const resyncing = reduce(at4, { type: "resyncing" });
    expect(reduce(resyncing, { type: "resumed", sequence: 4 }).phase).toBe("resyncing");

    // And a frame that already made it playable is left alone.
    const active = reduce(at4, { type: "resumed", sequence: 4 });
    expect(active.phase).toBe("active");
  });
});
