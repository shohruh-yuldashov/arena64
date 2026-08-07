import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { initialState, reduce } from "@/features/game/model/state";
import {
  RATING_MAX_ATTEMPTS,
  RATING_POLL_MS,
  useRatingResult,
} from "@/features/game/model/use-rating-result";
import { GameControls } from "@/features/game/ui/game-controls";
import { matchmakingKeys } from "@/features/matchmaking/api/keys";
import { httpClient } from "@/shared/api/client";
import { env } from "@/shared/config/env";
import { I18nProvider } from "@/shared/i18n";
import type { SnapshotPayload } from "@/shared/realtime";
import { RealtimeClient } from "@/shared/realtime";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp, renderWithProviders } from "@/shared/test/render";

/**
 * The control surface, rendered — A64-020.5C §21.
 *
 * Two tests, and they answer different questions. The first is about
 * **accessibility and visibility**: who sees which control, whether a
 * keyboard reaches it, and whether an arriving offer is announced. The
 * second is about **reachability**: that the panel is mounted in the real
 * page and its buttons put real frames on the real socket — §23 is explicit
 * that an isolated manually rendered button proves nothing.
 */

const VIEWER = "11111111-1111-1111-1111-111111111111";
const OPPONENT = "22222222-2222-2222-2222-222222222222";
const MATCH = "019fd1c7-5178-7a94-8076-4eeece03a8f4";

function snapshot(draw?: SnapshotPayload["draw"]): SnapshotPayload {
  return {
    match_id: MATCH,
    engine_version: 2,
    variant: "russian_8x8",
    status: "active",
    rated: true,
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
    ...(draw === undefined ? {} : { draw }),
    server_time: "2026-08-05T10:00:00Z",
  };
}

function stateWith(draw?: SnapshotPayload["draw"]) {
  return reduce(initialState(MATCH), {
    type: "snapshot",
    payload: snapshot(draw),
    viewerId: VIEWER,
  });
}

function mount(draw?: SnapshotPayload["draw"], onCommand = vi.fn()) {
  render(
    <I18nProvider>
      <GameControls state={stateWith(draw)} onCommand={onCommand} />
    </I18nProvider>,
  );
  return onCommand;
}

describe("the control surface", () => {
  it("offers each action only to the player the server permits, by keyboard", async () => {
    // §7 and §18. The asymmetry is the feature: showing Accept to the
    // player who *made* the offer would produce a request the server
    // refuses, and showing it to a spectator would leak a negotiation.
    const user = userEvent.setup();

    // --- the recipient ---------------------------------------------------
    const onCommand = mount({
      offer: { offered_by: "dark", offered_at_ply: 3, offered_at: "2026-08-05T09:59:00Z" },
      may_offer: false,
      may_accept: true,
      may_decline: true,
    });

    // Announced without the player having done anything — the one thing on
    // this panel that arrives unprompted, so it is `role="alert"`.
    const offer = await screen.findByRole("alert");
    expect(offer).toHaveTextContent(/offers a draw/i);

    const accept = screen.getByRole("button", { name: /accept draw/i });
    const decline = screen.getByRole("button", { name: /decline draw/i });
    // Focus is moved to Accept when the offer appears, so a keyboard user
    // is already on the control rather than tabbing from the board.
    await waitFor(() => expect(accept).toHaveFocus());

    // Reachable and operable by keyboard alone, not only by click.
    await user.keyboard("{Enter}");
    expect(onCommand).toHaveBeenCalledWith("accept");

    await user.tab();
    expect(decline).toHaveFocus();

    // The recipient may not open a competing offer while one stands.
    expect(screen.getByRole("button", { name: /offer a draw/i })).toBeDisabled();
  });

  it("hides the negotiation from the offerer and from a spectator", () => {
    // The offerer sees a durable sent state and no answer buttons — §6,
    // §16. A toast would satisfy neither.
    const { unmount } = render(
      <I18nProvider>
        <GameControls
          state={stateWith({
            offer: {
              offered_by: "light",
              offered_at_ply: 4,
              offered_at: "2026-08-05T10:00:00Z",
            },
            may_offer: false,
            may_accept: false,
            may_decline: false,
          })}
          onCommand={vi.fn()}
        />
      </I18nProvider>,
    );

    expect(screen.getByRole("status")).toHaveTextContent(/draw offer sent/i);
    expect(screen.queryByRole("button", { name: /accept draw/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /decline draw/i })).toBeNull();
    // Resigning is still available: an offer does not lock the board.
    expect(screen.getByRole("button", { name: /^resign$/i })).toBeEnabled();
    unmount();

    // A spectator's snapshot carries no `draw` block and names them no
    // side, so the whole panel is absent — not disabled, absent. A disabled
    // panel would tell a viewer that controls exist for them.
    const spectator = reduce(initialState(MATCH), {
      type: "snapshot",
      payload: snapshot(),
      viewerId: "33333333-3333-3333-3333-333333333333",
    });
    render(
      <I18nProvider>
        <GameControls state={spectator} onCommand={vi.fn()} />
      </I18nProvider>,
    );
    expect(screen.queryByRole("button", { name: /^resign$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /offer a draw/i })).toBeNull();
  });
});

// --- reachability — §23 ------------------------------------------------------

const url = (path: string) => `${env.VITE_API_URL}${path}`;
const envelope = <T,>(data: T) => ({ data, meta: { request_id: null, correlation_id: null } });

const SESSION = {
  id: VIEWER,
  username: "viewer",
  display_name: "Viewer",
  email: "viewer@example.com",
  is_active: true,
  is_verified: true,
};

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  httpClient.interceptors.request.clear();
  httpClient.interceptors.response.clear();
});

it("puts a real resign frame on the real socket from the real game page", async () => {
  // §23's requirement, and the one claim no unit test above can make: that
  // the panel is **mounted** and its buttons reach the transport. This
  // drives the whole graph — real router at `/games/{id}`, real providers,
  // real `RealtimeClient`, real `useGameRoom`, real reducer — and stubs
  // only `WebSocket`, so what is proven is that the app reaches the point
  // of sending a frame rather than that a mock was called.
  const sent: string[] = [];
  let onmessage: ((event: { data: string }) => void) | null = null;

  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: SESSION })),
    ),
    http.post(url("/auth/ws-ticket"), () =>
      HttpResponse.json(envelope({ ticket: "t1", expires_at: "2026-08-05T10:00:30Z" }), {
        status: 201,
      }),
    ),
  );

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
        // `ready` the moment it opens, so the room's join effect runs.
        queueMicrotask(() => {
          onmessage?.({
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
        sent.push(frame);
        // Answer `room.join` and `game.resume` so the room reaches
        // `active` — otherwise the page renders a skeleton and no control.
        const parsed = JSON.parse(frame) as { type: string; request_id?: string };
        if (parsed.type === "room.join") {
          onmessage?.({
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
          onmessage?.({
            data: JSON.stringify({
              v: 1,
              type: "game.snapshot",
              request_id: parsed.request_id ?? null,
              channel: "game",
              payload: snapshot({
                offer: null,
                may_offer: true,
                may_accept: false,
                may_decline: false,
              }),
            }),
          });
        }
      }
    },
  );

  renderApp({ path: `/games/${MATCH}`, realtimeClient: new RealtimeClient() });

  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: /^resign$/i }, { timeout: 5000 }));

  // The confirmation is required: clicking Resign must not resign.
  const dialog = await screen.findByRole("alertdialog");
  expect(dialog).toHaveTextContent(/opponent wins/i);
  expect(sent.some((frame) => frame.includes("game.resign"))).toBe(false);

  await user.click(
    (await screen.findAllByRole("button", { name: /^resign$/i })).at(-1) as HTMLElement,
  );

  await waitFor(() => {
    const resign = sent
      .map((f) => JSON.parse(f) as Record<string, unknown>)
      .find((f) => f.type === "game.resign");
    expect(resign).toBeDefined();
    // §19: `match_id` and nothing else. No side, no player id, no outcome —
    // asserted against the frame that actually left, which is the only
    // place this can be checked rather than reviewed.
    expect(resign?.payload).toEqual({ match_id: MATCH });
    expect(resign?.channel).toBe("game");
    // Correlated through the existing registry, not a second token.
    expect(typeof resign?.request_id).toBe("string");
  });
});

it("stops treating a finished match as the current one", async () => {
  // **The lobby bug, at the level it actually lives.**
  //
  // `GET /matchmaking/matches/pending` answers with a match that is
  // `pending_acceptance` **or `active`**, with no time window. The backend
  // excludes a *completed* match correctly; what went wrong was that
  // nothing on this side cleared the cached copy when a game ended.
  //
  // The copy left behind still said `active`, and an invalidated query
  // still serves its stale value while it refetches — so pressing "Back to
  // lobby" put the player back into the game they had just finished, where
  // `room.join` is refused and the page renders "That game could not be
  // opened".
  let onmessage: ((event: { data: string }) => void) | null = null;

  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: SESSION })),
    ),
    http.post(url("/auth/ws-ticket"), () =>
      HttpResponse.json(envelope({ ticket: "t1", expires_at: "2026-08-05T10:00:30Z" }), {
        status: 201,
      }),
    ),
  );

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
      constructor() {
        queueMicrotask(() => {
          onmessage?.({
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
        const parsed = JSON.parse(frame) as { type: string; request_id?: string };
        if (parsed.type === "room.join") {
          onmessage?.({
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
          onmessage?.({
            data: JSON.stringify({
              v: 1,
              type: "game.snapshot",
              request_id: parsed.request_id ?? null,
              channel: "game",
              payload: snapshot({
                offer: null,
                may_offer: true,
                may_accept: false,
                may_decline: false,
              }),
            }),
          });
        }
      }
    },
  );

  const { queryClient } = renderApp({
    path: `/games/${MATCH}`,
    realtimeClient: new RealtimeClient(),
  });

  // What the lobby left behind on its way into this game.
  queryClient.setQueryData(matchmakingKeys.pending(), {
    match_id: MATCH,
    status: "active",
    you_accepted: true,
    opponent: null,
    rated: true,
    speed_class: "blitz",
    base_time_ms: 180_000,
    increment_ms: 2_000,
    acceptance_deadline: "2026-08-05T10:00:30Z",
  });

  await screen.findByRole("button", { name: /^resign$/i }, { timeout: 5000 });

  // Through a closure, so the narrowing that would make `onmessage` look
  // like `never` here — it is only ever assigned from inside the stub — does
  // not apply.
  const deliver = (frame: unknown) => onmessage?.({ data: JSON.stringify(frame) });
  deliver({
    v: 1,
    type: "game.completed",
    channel: "game",
    payload: {
      match_id: MATCH,
      ply: 12,
      result: { outcome: "win", winner: "light", termination_reason: "resignation" },
    },
  });

  // The finished match is no longer the current one. `undefined`, not a
  // stale object — the lobby has nothing to navigate on until the server
  // answers again.
  await waitFor(() =>
    expect(queryClient.getQueryData(matchmakingKeys.pending())).toBeUndefined(),
  );
});

describe("the clock follows the move — A64-024", () => {
  /**
   * The bug: `game.move.applied` carried no clock, so the reducer left
   * `state.clock` on whatever the last snapshot said — and the countdown
   * kept running for the player who had just moved. Reloading fetched a
   * snapshot, which is why a reload "fixed" it.
   *
   * Asserted on the reducer, because that is where ownership is held. The
   * interpolation above it reads `clock.active_side` and does not decide.
   */
  function moveFrame(overrides: Record<string, unknown> = {}) {
    return {
      match_id: MATCH,
      ply: 5,
      side_to_move: "dark" as const,
      fingerprint: "fp5",
      applied: { path: ["c3", "d4"], captured: [], promoted_to: null },
      clock: {
        light_ms: 170_000,
        dark_ms: 180_000,
        active_side: "dark" as const,
        deadline: "2026-08-05T10:03:00Z",
        server_time: "2026-08-05T10:00:10Z",
      },
      ...overrides,
    };
  }

  /** A snapshot with Light's clock running — the fixture above is untimed. */
  function timed() {
    return reduce(initialState(MATCH), {
      type: "snapshot",
      viewerId: VIEWER,
      payload: {
        ...snapshot(),
        clock: {
          light_ms: 180_000,
          dark_ms: 180_000,
          active_side: "light" as const,
          deadline: "2026-08-05T10:03:00Z",
          server_time: "2026-08-05T10:00:00Z",
        },
      },
    });
  }

  it("hands the running clock to the opponent when Light moves", () => {
    // The snapshot says Light is counting.
    const before = timed();
    expect(before.clock?.active_side).toBe("light");

    const after = reduce(before, { type: "applied", payload: moveFrame() });

    expect(after.sideToMove).toBe("dark");
    // The point of the fix: ownership moved with the move.
    expect(after.clock?.active_side).toBe("dark");
    expect(after.clock?.light_ms).toBe(170_000);
  });

  it("hands it back when Dark moves", () => {
    const afterLight = reduce(timed(), { type: "applied", payload: moveFrame() });
    const afterDark = reduce(afterLight, {
      type: "applied",
      payload: moveFrame({
        ply: 6,
        side_to_move: "light",
        fingerprint: "fp6",
        clock: {
          light_ms: 170_000,
          dark_ms: 168_000,
          active_side: "light",
          deadline: "2026-08-05T10:05:50Z",
          server_time: "2026-08-05T10:00:22Z",
        },
      }),
    });

    expect(afterDark.clock?.active_side).toBe("light");
    expect(afterDark.clock?.dark_ms).toBe(168_000);
  });

  it("re-anchors rather than accumulating local elapsed time", () => {
    // `server_time` moves with the frame, which is what `useClock` measures
    // its offset against. A reducer that kept the old clock would leave the
    // anchor at the snapshot's instant and count from there forever.
    const after = reduce(timed(), { type: "applied", payload: moveFrame() });

    expect(after.clock?.server_time).toBe("2026-08-05T10:00:10Z");
    expect(after.clock?.deadline).toBe("2026-08-05T10:03:00Z");
  });

  it("keeps the clock it has when the frame carries none", () => {
    // An untimed match, or a server that predates the field. Blanking would
    // stop a clock this client can still count correctly.
    const before = timed();
    const after = reduce(before, {
      type: "applied",
      payload: moveFrame({ clock: undefined }),
    });

    expect(after.clock).toEqual(before.clock);
  });

  it("gives a resumed snapshot the same ownership a live move does", () => {
    // §4: no reload-only correction path. The two routes to "Dark is
    // counting" must agree.
    const live = reduce(timed(), { type: "applied", payload: moveFrame() });
    const resumed = reduce(timed(), {
      type: "snapshot",
      viewerId: VIEWER,
      payload: {
        ...snapshot(),
        sequence: 5,
        side_to_move: "dark",
        fingerprint: "fp5",
        clock: moveFrame().clock,
      },
    });

    expect(resumed.clock?.active_side).toBe(live.clock?.active_side);
    expect(resumed.sideToMove).toBe(live.sideToMove);
  });
});

describe("the rated result reconciles within a bound — A64-024", () => {
  /**
   * The bug: the first version used TanStack's `refetchInterval` and stuck
   * on "Rating is being updated…" forever in a real browser — its schedule
   * depends on document visibility, observer lifetime and an attempt
   * counter on the cache entry, and it could not be tested at all because
   * jsdom never reports the document visible.
   *
   * The loop is now a plain effect with a timeout chain, so every one of
   * these assertions is about the hook rather than about the environment.
   */
  function row(overrides: Record<string, unknown>) {
    return {
      match_id: MATCH,
      variant: "russian_8x8",
      speed_class: "blitz",
      rated: true,
      engine_version: 2,
      light_player_id: VIEWER,
      dark_player_id: OPPONENT,
      opponent_id: OPPONENT,
      outcome: "win",
      termination_reason: "resignation",
      winner: "light",
      ply_number: 24,
      opponent: null,
      time_control: null,
      rating: null,
      started_at: "2026-08-05T10:00:00Z",
      ended_at: "2026-08-05T10:05:00Z",
      ...overrides,
    };
  }

  function Probe() {
    const rating = useRatingResult({ matchId: MATCH, viewerId: VIEWER, enabled: true });
    return (
      <p>
        {JSON.stringify({
          delta: rating.change?.delta ?? null,
          rated: rating.rated,
          pending: rating.isPending,
          late: rating.hasGivenUp,
        })}
      </p>
    );
  }

  /** Serves a page per call, and counts the calls. */
  function serve(pages: Record<string, unknown>[]): { calls: () => number } {
    let calls = 0;
    mswServer.use(
      http.get(url(`/players/${VIEWER}/matches`), () => {
        const page = pages[Math.min(calls, pages.length - 1)];
        calls += 1;
        return HttpResponse.json(envelope({ entries: [page], next_cursor: null }));
      }),
    );
    return { calls: () => calls };
  }

  it("renders the delta as soon as the projection lands", async () => {
    // Absent on the first answer, present on the second — the ordinary
    // shape of an outbox consumer catching up.
    const served = serve([
      row({ rating: null }),
      row({ rating: { before: 1524, after: 1537, delta: 13 } }),
    ]);
    renderWithProviders(<Probe />);

    await waitFor(() => expect(screen.getByText(/"pending":true/)).toBeVisible());
    await waitFor(() => expect(screen.getByText(/"delta":13/)).toBeVisible(), {
      timeout: RATING_POLL_MS * 3,
    });
    expect(screen.getByText(/"pending":false/)).toBeVisible();

    // And it stops the moment the answer arrives.
    const spent = served.calls();
    await new Promise((resolve) => setTimeout(resolve, RATING_POLL_MS * 2));
    expect(served.calls()).toBe(spent);
  }, 15_000);

  it("asks once for a casual match and never waits", async () => {
    const served = serve([row({ rated: false, rating: null })]);
    renderWithProviders(<Probe />);

    await waitFor(() => expect(screen.getByText(/"rated":false/)).toBeVisible());
    expect(screen.getByText(/"pending":false/)).toBeVisible();
    expect(screen.getByText(/"late":false/)).toBeVisible();

    await new Promise((resolve) => setTimeout(resolve, RATING_POLL_MS * 2));
    expect(served.calls()).toBe(1);
  }, 15_000);

  it("stops after a bounded number of attempts instead of waiting forever", async () => {
    // Every answer says the adjustment is still absent. This is the case
    // that hung: the UI must stop asking and say so.
    const served = serve([row({ rating: null })]);
    renderWithProviders(<Probe />);

    await waitFor(() => expect(screen.getByText(/"late":true/)).toBeVisible(), {
      timeout: RATING_POLL_MS * (RATING_MAX_ATTEMPTS + 2),
    });
    expect(screen.getByText(/"pending":false/)).toBeVisible();
    expect(served.calls()).toBe(RATING_MAX_ATTEMPTS);

    // Genuinely stopped, not merely reporting that it had.
    await new Promise((resolve) => setTimeout(resolve, RATING_POLL_MS * 2));
    expect(served.calls()).toBe(RATING_MAX_ATTEMPTS);
  }, 30_000);
});
