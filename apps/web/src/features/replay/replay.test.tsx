import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";

import { httpClient } from "@/shared/api/client";
import { env } from "@/shared/config/env";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * The replay, through the real app — A64-020.5E §27.
 *
 * Every test below mounts the **real router** at
 * `/games/{id}/replay`, so the route registration, the guard, the query,
 * the shared board and the navigation are all exercised together. §28 is
 * explicit that an isolated replay component proves nothing; what is
 * substituted here is the HTTP layer and nothing else.
 */

const url = (path: string) => `${env.VITE_API_URL}${path}`;
const envelope = <T,>(data: T) => ({ data, meta: { request_id: null, correlation_id: null } });
/** The platform's error body — `code` **and** `message`, which is what
 *  `asErrorBody` requires before it will surface a code at all. */
const problem = (status: number, code: string) =>
  HttpResponse.json(
    { code, message: "Nope.", request_id: null, correlation_id: null },
    { status },
  );

const VIEWER = {
  id: "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
  username: "viewer",
  display_name: "Viewer",
  email: "viewer@example.com",
  is_active: true,
  is_verified: true,
};
const OPPONENT_ID = "019fd0cc-3333-7000-8000-000000000003";
const MATCH = "019fd0bb-2222-7000-8000-000000000002";

/** The opening, and two plies — the second a double capture that crowns. */
function replay(overrides: Record<string, unknown> = {}) {
  return {
    match_id: MATCH,
    variant: "russian_8x8",
    engine_version: 2,
    status: "completed",
    rated: true,
    speed_class: "blitz",
    time_control: { initial_ms: 180_000, increment_ms: 2_000 },
    light: {
      player_id: VIEWER.id,
      username: "viewer",
      display_name: "Viewer",
      avatar_thumbnail_url: null,
      rating_value: 1500,
      rating_deviation: 80,
      is_provisional: false,
    },
    dark: {
      player_id: OPPONENT_ID,
      username: "rival",
      display_name: "Rival",
      avatar_thumbnail_url: null,
      rating_value: 1620,
      rating_deviation: 60,
      is_provisional: false,
    },
    created_at: "2026-08-05T10:00:00Z",
    ended_at: "2026-08-05T10:04:00Z",
    opening: [
      { square: "c3", side: "light", rank: "man" },
      { square: "d4", side: "dark", rank: "man" },
      { square: "f6", side: "dark", rank: "man" },
    ],
    plies: [
      {
        ply_number: 1,
        side: "light",
        path: ["c3", "e5"],
        captured: ["d4"],
        promoted_to: null,
        pieces: [
          { square: "e5", side: "light", rank: "man" },
          { square: "f6", side: "dark", rank: "man" },
        ],
        fingerprint: "fp1",
        think_time_ms: 4000,
        remaining_clock_ms: 176_000,
      },
      {
        ply_number: 2,
        side: "dark",
        path: ["f6", "d4", "b2"],
        captured: ["e5", "c3"],
        promoted_to: "king",
        pieces: [{ square: "b2", side: "dark", rank: "king" }],
        fingerprint: "fp2",
        think_time_ms: 3000,
        remaining_clock_ms: 179_000,
      },
    ],
    outcome: "win",
    termination_reason: "resignation",
    winner: "dark",
    ...overrides,
  };
}

let replayReads = 0;
let profileReads = 0;

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  httpClient.interceptors.request.clear();
  httpClient.interceptors.response.clear();
  replayReads = 0;
  profileReads = 0;
  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: VIEWER })),
    ),
    // Any profile read at all is a failure of §13/§23 — the replay
    // response already carries both seats.
    http.get(url("/users/:id"), () => {
      profileReads += 1;
      return problem(404, "not_found");
    }),
  );
});

function serveReplay(body: Record<string, unknown> = replay()) {
  mswServer.use(
    http.get(url(`/matches/${MATCH}/replay`), () => {
      replayReads += 1;
      return HttpResponse.json(envelope(body));
    }),
  );
}

it("steps through positions with the board, the move list and the URL in agreement", async () => {
  // §27.2 and §27.3 together, because the second is only checkable while
  // navigating: the board must show the *server's* position at every
  // index, and the move list must highlight the ply that produced it.
  serveReplay();
  const user = userEvent.setup();
  renderApp({ path: `/games/${MATCH}/replay` });

  // The opening — before any move. `d4` is still occupied here and gone
  // after ply 1, which is what makes this an assertion about the position
  // rather than about the page having rendered.
  const board = await screen.findByRole("grid", { name: /draughts board/i }, { timeout: 5000 });
  await waitFor(() =>
    expect(within(board).getByRole("gridcell", { name: /^d4, Dark, man/ })).toBeInTheDocument(),
  );
  expect(screen.getByRole("button", { name: /beginning/i })).toBeDisabled();

  // Forward one ply: the light man capturing d4 and landing on e5.
  await user.click(screen.getByRole("button", { name: /^next$/i }));
  await waitFor(() =>
    expect(
      within(board).getByRole("gridcell", { name: /^e5, Light, man/ }),
    ).toBeInTheDocument(),
  );
  expect(within(board).queryByRole("gridcell", { name: /^d4, Dark, man/ })).toBeNull();

  // The move list agrees, and marks it as the step being viewed.
  const first = screen.getByRole("button", { name: /c3–e5/ });
  expect(first).toHaveAttribute("aria-current", "step");

  // Forward again: the multi-capture, rendered as its **whole path** —
  // f6–d4–b2 rather than f6–b2, because two sequences can share endpoints
  // and which pieces came off is the point (§9).
  await user.click(screen.getByRole("button", { name: /^next$/i }));
  const second = screen.getByRole("button", { name: /f6–d4–b2/ });
  expect(second).toHaveAttribute("aria-current", "step");
  // And the promotion the server reported, from its own board.
  await waitFor(() =>
    expect(
      within(board).getByRole("gridcell", { name: /^b2, Dark, king/ }),
    ).toBeInTheDocument(),
  );
  expect(screen.getByRole("button", { name: /^end$/i })).toBeDisabled();

  // Clicking a move list entry jumps back, and the board follows.
  await user.click(first);
  await waitFor(() =>
    expect(
      within(board).getByRole("gridcell", { name: /^e5, Light, man/ }),
    ).toBeInTheDocument(),
  );

  // §23: one replay request for the whole game, whatever the navigation.
  expect(replayReads).toBe(1);
  expect(profileReads).toBe(0);
});

it("answers the keyboard but never while a field has the caret", async () => {
  // §27.5. The second half is the one worth having: without the guard,
  // typing in any field on a page above this one would step the board.
  serveReplay();
  const user = userEvent.setup();
  renderApp({ path: `/games/${MATCH}/replay` });

  const board = await screen.findByRole("grid", { name: /draughts board/i }, { timeout: 5000 });
  await waitFor(() =>
    expect(within(board).getByRole("gridcell", { name: /^d4, Dark, man/ })).toBeInTheDocument(),
  );

  await user.keyboard("{ArrowRight}");
  await waitFor(() =>
    expect(
      within(board).getByRole("gridcell", { name: /^e5, Light, man/ }),
    ).toBeInTheDocument(),
  );

  await user.keyboard("{End}");
  await waitFor(() =>
    expect(
      within(board).getByRole("gridcell", { name: /^b2, Dark, king/ }),
    ).toBeInTheDocument(),
  );

  await user.keyboard("{Home}");
  await waitFor(() =>
    expect(within(board).getByRole("gridcell", { name: /^d4, Dark, man/ })).toBeInTheDocument(),
  );

  // A field on the page swallows the key. Added to the document rather
  // than mocked, so what is exercised is the real listener's real guard.
  const field = document.createElement("input");
  document.body.append(field);
  field.focus();
  await user.keyboard("{ArrowRight}");
  expect(within(board).getByRole("gridcell", { name: /^d4, Dark, man/ })).toBeInTheDocument();
  field.remove();
});

it("keeps a refused engine version distinct from a match it may not see", async () => {
  // §27.6 and §16, §17. Two different states, and conflating them would be
  // wrong in both directions: an unsupported version is not a missing
  // match, and a hidden match must not be told apart from a missing one.
  mswServer.use(
    http.get(url(`/matches/${MATCH}/replay`), () => problem(409, "unsupported_engine_version")),
  );
  renderApp({ path: `/games/${MATCH}/replay` });

  const refusal = await screen.findByRole("alert", undefined, { timeout: 5000 });
  expect(refusal).toHaveTextContent(/cannot be replayed/i);
  // No board is shown. An empty one pretending to be a position is the
  // failure §16 refuses outright.
  expect(screen.queryByRole("grid", { name: /draughts board/i })).toBeNull();
  // Not retryable: the answer is stable and about a permanent record.
  expect(screen.queryByRole("button", { name: /try again/i })).toBeNull();
});

it("says nothing about a match it was not shown", async () => {
  // §17 and §24. The copy must not confirm the match exists, and it must
  // not name anybody — a screen that said "you do not have permission"
  // would leak exactly what the shared `404` exists to hide.
  mswServer.use(http.get(url(`/matches/${MATCH}/replay`), () => problem(404, "not_found")));
  renderApp({ path: `/games/${MATCH}/replay` });

  const refusal = await screen.findByRole("alert", undefined, { timeout: 5000 });
  expect(refusal).toHaveTextContent(/not available/i);
  expect(refusal).not.toHaveTextContent(/permission|private|rival/i);
  expect(screen.queryByRole("grid", { name: /draughts board/i })).toBeNull();
});

it("renders a completed game that nobody moved in", async () => {
  // §18: zero plies is a valid replay, not an empty state. The opening is
  // the final position, the result still stands, and both navigation
  // boundaries are the same place.
  serveReplay(replay({ plies: [], termination_reason: "abort", outcome: null, winner: null }));
  renderApp({ path: `/games/${MATCH}/replay` });

  const board = await screen.findByRole("grid", { name: /draughts board/i }, { timeout: 5000 });
  expect(within(board).getByRole("gridcell", { name: /^c3, Light, man/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /beginning/i })).toBeDisabled();
  expect(screen.getByRole("button", { name: /^end$/i })).toBeDisabled();
  expect(screen.getByText(/no moves were played/i)).toBeInTheDocument();
  // The metadata is still there — which is what "not an empty state" means.
  expect(screen.getByRole("alert")).toBeInTheDocument();
});
