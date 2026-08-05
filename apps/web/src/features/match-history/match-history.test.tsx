import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";

import { httpClient } from "@/shared/api/client";
import { env } from "@/shared/config/env";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * Match history, through the real app — A64-020.5F §29.
 *
 * Every test mounts the **real router** at `/games/history`, so the route
 * registration, the guard, the infinite query and the replay links are
 * exercised together. §13 is explicit that an isolated component proves
 * nothing; what is substituted here is the HTTP layer and nothing else.
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
const OPPONENT_ID = "019fd0cc-3333-7000-8000-000000000003";

function entry(index: number, overrides: Record<string, unknown> = {}) {
  return {
    match_id: `019fd0bb-2222-7000-8000-00000000${String(index).padStart(4, "0")}`,
    variant: "russian_8x8",
    speed_class: "blitz",
    rated: true,
    engine_version: 2,
    light_player_id: VIEWER.id,
    dark_player_id: OPPONENT_ID,
    opponent_id: OPPONENT_ID,
    opponent: {
      player_id: OPPONENT_ID,
      username: "rival",
      display_name: "Rival",
      avatar_thumbnail_url: null,
      rating_value: null,
      rating_deviation: null,
      is_provisional: null,
    },
    time_control: { initial_ms: 180_000, increment_ms: 2_000 },
    outcome: "win",
    termination_reason: "resignation",
    winner: "light",
    ply_number: 24,
    started_at: "2026-08-05T10:00:00Z",
    ended_at: "2026-08-05T10:04:00Z",
    ...overrides,
  };
}

let historyRequests: string[] = [];
let profileRequests = 0;

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  httpClient.interceptors.request.clear();
  httpClient.interceptors.response.clear();
  historyRequests = [];
  profileRequests = 0;
  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: VIEWER })),
    ),
    // Any per-row profile read is a failure of §17 and §27 — the opponent
    // arrives composed in the history response.
    http.get(url("/users/:id"), () => {
      profileRequests += 1;
      return HttpResponse.json(envelope({}));
    }),
    http.get(url("/profiles/:username"), () => {
      profileRequests += 1;
      return HttpResponse.json(envelope({}));
    }),
  );
});

/** Two pages: three matches, then two, then the end. */
function servePages() {
  mswServer.use(
    http.get(url(`/players/${VIEWER.id}/matches`), ({ request }) => {
      const after = new URL(request.url).searchParams.get("after");
      historyRequests.push(after ?? "first");
      if (after === null) {
        return HttpResponse.json(
          envelope({ entries: [entry(1), entry(2), entry(3)], next_cursor: "cursor-2" }),
        );
      }
      return HttpResponse.json(envelope({ entries: [entry(4), entry(5)], next_cursor: null }));
    }),
  );
}

it("pages through the cursor, showing every match once and asking nothing per row", async () => {
  // §29.9 and §29.10 together, because the second is only checkable while
  // paging: a client that rebuilt the cursor, or re-sent the first one,
  // would duplicate rows — and one that fetched a profile per row would
  // pass a rendering assertion while issuing ten requests.
  servePages();
  const user = userEvent.setup();
  renderApp({ path: "/games/history" });

  const list = await screen.findByRole("list", { name: /match history/i }, { timeout: 5000 });
  await waitFor(() => expect(within(list).getAllByRole("listitem")).toHaveLength(3));

  // One request for the page, and none for the three opponents.
  expect(historyRequests).toEqual(["first"]);
  expect(profileRequests).toBe(0);

  await user.click(screen.getByRole("button", { name: /load more/i }));

  await waitFor(() => expect(within(list).getAllByRole("listitem")).toHaveLength(5));
  // The **opaque cursor, sent back verbatim** — not decoded, not rebuilt.
  expect(historyRequests).toEqual(["first", "cursor-2"]);
  expect(profileRequests).toBe(0);

  // Every match exactly once: five rows, five distinct replay links.
  const links = within(list).getAllByRole("link", { name: /replay the match/i });
  expect(links).toHaveLength(5);
  expect(new Set(links.map((link) => link.getAttribute("href")))).toHaveLength(5);

  // The last page said `next_cursor: null`, so the chain stops and says so
  // rather than offering a button that would re-fetch the same page.
  expect(screen.queryByRole("button", { name: /load more/i })).toBeNull();
  expect(screen.getByText(/that is every match/i)).toBeInTheDocument();
});

it("links each row to the real replay route and names it unambiguously", async () => {
  // §29.11 and §23. The accessible name is the assertion worth having:
  // "Replay" repeated down a list gives a screen reader twenty identical
  // links, and naming the opponent is what makes each distinguishable.
  servePages();
  renderApp({ path: "/games/history" });

  const list = await screen.findByRole("list", { name: /match history/i }, { timeout: 5000 });
  const first = within(list).getAllByRole("listitem")[0] as HTMLElement;

  const link = within(first).getByRole("link", { name: /replay the match against rival/i });
  expect(link).toHaveAttribute("href", "/games/019fd0bb-2222-7000-8000-000000000001/replay");

  // The result is a word, not only a colour — §23.
  expect(within(first).getByText(/^won$/i)).toBeInTheDocument();
  // And the reason is the server's vocabulary, mapped: `resignation`
  // renders as a sentence rather than as "Unknown", which is the mistake
  // A64-020.5B made with three invented reason names.
  expect(within(first).getByText(/resignation/i)).toBeInTheDocument();
});

it("tells a player with no matches what to do about it", async () => {
  // §21: helpful, not "No data". The link is the point — a player with an
  // empty history is a player who has not played, and the useful thing to
  // give them is the way to.
  mswServer.use(
    http.get(url(`/players/${VIEWER.id}/matches`), () =>
      HttpResponse.json(envelope({ entries: [], next_cursor: null })),
    ),
  );
  renderApp({ path: "/games/history" });

  expect(
    await screen.findByText(/no matches yet/i, undefined, { timeout: 5000 }),
  ).toBeVisible();
  expect(screen.getByRole("link", { name: /find a game/i })).toHaveAttribute("href", "/play");
  expect(screen.queryByRole("list", { name: /match history/i })).toBeNull();
});

it("turns an anonymous visitor away rather than rendering the list", async () => {
  // §29.8. The guard is asserted through the **real** router, because a
  // route added later without `protectedPage` renders a page that calls an
  // authenticated endpoint, gets a `401`, and looks like a loading failure
  // rather than a missing guard.
  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json({ code: "invalid_token", message: "No." }, { status: 401 }),
    ),
  );
  const { unmount } = renderApp({ path: "/games/history" });

  // The sign-in page, asserted the way `matchmaking.test.tsx` asserts it:
  // `renderApp` drives a memory history, so `window.location` never moves
  // and what proves the redirect is what rendered.
  expect(await screen.findByRole("heading", { name: /kirish|sign in|вход/i })).toBeVisible();
  expect(screen.queryByRole("list", { name: /match history/i })).toBeNull();

  // Explicit, like the other guard tests: the router begins loading the
  // history chunk before the redirect lands, and letting that resolve into
  // a torn-down root is an unhandled rejection unrelated to the assertion.
  unmount();
});
