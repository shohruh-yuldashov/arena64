import { screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";

import { env } from "@/shared/config/env";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * The four notification types A64-021.4 added, rendered and navigable — §32.
 *
 * Through the **real router** at `/notifications`, so route registration,
 * the guard, the query layer, the message mapper and the target mapper are
 * exercised together. §33: a component rendered in isolation is not a
 * reachability proof; what is substituted here is the HTTP layer alone.
 *
 * Two tests, and they are two different claims:
 *
 *   every new type produces a **sentence a player can read** and a link
 *   that goes to the right route — the mapper is closed, so a missing
 *   branch renders the generic sentence and would fail this
 *
 *   a result the client cannot name, and a type it has never heard of,
 *   still render **safely** — which is the property that lets the backend
 *   ship a type before the frontend knows it
 */

const url = (path: string) => `${env.VITE_API_URL}${path}`;
const envelope = <T,>(data: T) => ({ data, meta: { request_id: null, correlation_id: null } });

const VIEWER = {
  id: "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
  username: "viewer",
  display_name: "Viewer",
  email: "viewer@example.com",
  is_active: true,
  is_verified: true,
};

const TOURNAMENT_ID = "019fe500-0000-7000-8000-0000000000a1";
const MATCH_ID = "019fe500-0000-7000-8000-0000000000b2";
const CHALLENGE_ID = "019fe500-0000-7000-8000-0000000000c3";

const OPPONENT = {
  player_id: "019fb9ea-0a0c-7cec-9c5f-402727c31b01",
  username: "rival",
  display_name: "Rival",
  thumbnail_url: null,
};

function notification(overrides: Record<string, unknown>) {
  return {
    id: "019fe400-0000-7000-8000-000000000001",
    type: "tournament_registration_confirmed",
    category: "tournament",
    actor: null,
    tournament: null,
    game: null,
    challenge: null,
    target: { type: "tournament", ref: TOURNAMENT_ID },
    created_at: "2026-08-07T10:00:00Z",
    read_at: null,
    is_read: false,
    ...overrides,
  };
}

const TOURNAMENT = {
  tournament_id: TOURNAMENT_ID,
  tournament_name: "Sunday Open",
  round_number: null,
  final_rank: null,
};

function signedIn(): void {
  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: VIEWER })),
    ),
  );
}

function serve(entries: unknown[]): void {
  mswServer.use(
    http.get(url("/notifications"), () =>
      HttpResponse.json(envelope({ entries, next_cursor: null })),
    ),
    http.get(url("/notifications/unread-count"), () =>
      HttpResponse.json(envelope({ unread_count: entries.length })),
    ),
  );
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  signedIn();
});

it("renders every new type and links each to its own route", async () => {
  serve([
    notification({
      id: "019fe400-0000-7000-8000-000000000001",
      type: "tournament_registration_confirmed",
      tournament: TOURNAMENT,
    }),
    notification({
      id: "019fe400-0000-7000-8000-000000000002",
      type: "tournament_round_published",
      tournament: { ...TOURNAMENT, round_number: 3 },
    }),
    notification({
      id: "019fe400-0000-7000-8000-000000000003",
      type: "tournament_completed",
      tournament: { ...TOURNAMENT, final_rank: 5 },
    }),
    notification({
      id: "019fe400-0000-7000-8000-000000000004",
      type: "game_completed",
      category: "game",
      tournament: null,
      game: {
        match_id: MATCH_ID,
        outcome: "win",
        termination_reason: "resignation",
        opponent: {
          player_id: "019fb9ea-0a0c-7cec-9c5f-402727c31b01",
          username: "rival",
          display_name: "Rival",
          thumbnail_url: null,
        },
      },
      target: { type: "match_replay", ref: MATCH_ID },
    }),
  ]);

  renderApp({ path: "/notifications" });

  const list = await screen.findByRole("list", { name: /notifications/i });

  // A sentence each, translated, with the server's facts interpolated — and
  // never a string the server composed.
  expect(within(list).getByText("You are entered in Sunday Open")).toBeVisible();
  expect(within(list).getByText("Round 3 of Sunday Open has been paired")).toBeVisible();
  expect(within(list).getByText("Sunday Open has finished — you placed 5")).toBeVisible();
  expect(within(list).getByText("You beat Rival")).toBeVisible();

  // The closed target mapper, exercised through the rendered links. A `ref`
  // becomes a route parameter here and nowhere else, which is what stops a
  // server-supplied string ever being a destination.
  const hrefs = within(list)
    .getAllByRole("link")
    .map((link) => link.getAttribute("href"));
  expect(hrefs).toEqual([
    `/tournaments/${TOURNAMENT_ID}`,
    `/tournaments/${TOURNAMENT_ID}`,
    `/tournaments/${TOURNAMENT_ID}`,
    `/games/${MATCH_ID}/replay`,
  ]);
});

it("renders both challenge types and links an accepted one to its game", async () => {
  // A64-022.4 §19, §22. The challenge surface belongs to A64-022.5, but
  // `/notifications` must render these the day the backend can write them —
  // a durable row nobody can read is a row that should not have been kept.
  serve([
    notification({
      id: "019fe400-0000-7000-8000-000000000007",
      type: "friend_challenge_received",
      category: "social",
      tournament: null,
      challenge: {
        challenge_id: CHALLENGE_ID,
        opponent: OPPONENT,
        time_control_id: "blitz_3_2",
        variant: "russian_8x8",
        rated: true,
        expires_at: "2026-08-08T10:00:00Z",
        match_id: null,
      },
      // A64-022.5. The list where it can be answered — the same
      // destination the service worker resolves for the same type.
      target: { type: "challenges", ref: null },
    }),
    notification({
      id: "019fe400-0000-7000-8000-000000000008",
      type: "friend_challenge_accepted",
      category: "social",
      tournament: null,
      challenge: {
        challenge_id: CHALLENGE_ID,
        opponent: OPPONENT,
        time_control_id: "blitz_3_2",
        variant: "russian_8x8",
        rated: true,
        expires_at: null,
        match_id: MATCH_ID,
      },
      target: { type: "live_game", ref: MATCH_ID },
    }),
  ]);

  renderApp({ path: "/notifications" });

  const list = await screen.findByRole("list", { name: /notifications/i });

  // The name is in the sentence here and deliberately **not** in the push
  // body: this is behind a session, and a lock screen is not.
  expect(within(list).getByText("Rival challenged you to a game")).toBeVisible();
  expect(within(list).getByText("Rival accepted your challenge")).toBeVisible();

  const hrefs = within(list)
    .getAllByRole("link")
    .map((link) => link.getAttribute("href"));
  // The handoff: the challenger reaches the created game in one tap, which
  // matters because the join window is ten minutes.
  expect(hrefs).toEqual(["/challenges", `/games/${MATCH_ID}`]);
});

it("degrades safely for an unnamed opponent, an unknown type and a missing ref", async () => {
  serve([
    notification({
      id: "019fe400-0000-7000-8000-000000000005",
      type: "game_completed",
      category: "game",
      // The opponent's account is gone. The game was still played, so the
      // row keeps the result and loses the name.
      game: {
        match_id: MATCH_ID,
        outcome: "loss",
        termination_reason: "abandonment",
        opponent: null,
      },
      target: { type: "match_replay", ref: MATCH_ID },
    }),
    notification({
      id: "019fe400-0000-7000-8000-00000000000b",
      type: "friend_challenge_received",
      category: "social",
      tournament: null,
      // **The retired target** — A64-022.4 wrote it on every received
      // challenge before the surface existed, and rows still hold it. It
      // must keep resolving, because a notification is history and
      // rewriting where an old one leads would be worse than leaving it
      // truthful.
      challenge: {
        challenge_id: CHALLENGE_ID,
        opponent: OPPONENT,
        time_control_id: "blitz_3_2",
        variant: "russian_8x8",
        rated: false,
        expires_at: "2026-08-08T10:00:00Z",
        match_id: null,
      },
      target: { type: "friends", ref: null },
    }),
    notification({
      id: "019fe400-0000-7000-8000-000000000006",
      // A type this build has never heard of — a backend that shipped ahead
      // of this client. It must still be a readable row.
      type: "tournament_match_ready",
      tournament: TOURNAMENT,
      target: { type: "live_game", ref: null },
    }),
  ]);

  renderApp({ path: "/notifications" });

  const list = await screen.findByRole("list", { name: /notifications/i });

  // Not "You lost to " — the sentence without a name is its own string.
  expect(within(list).getByText("You lost your game")).toBeVisible();
  expect(within(list).getByText("New notification")).toBeVisible();

  // The unknown type's target names no identifier, so its row is **not** a
  // link: a notification that cannot be navigated from is still worth
  // reading, and a link that goes nowhere is worse than none.
  // Two links: the completed game, and the retired-target challenge row.
  const hrefs = within(list)
    .getAllByRole("link")
    .map((link) => link.getAttribute("href"));
  expect(hrefs).toEqual([`/games/${MATCH_ID}/replay`, "/friends"]);
});
