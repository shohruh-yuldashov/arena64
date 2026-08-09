import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { App } from "@/app/App";
import { createAdminRouter } from "@/app/router";
import { accessToken } from "@/app/session-store";

/**
 * The Tournaments console — A64-024.5 §24.
 *
 * Three tests through the real router. The one that earns its place is the
 * bracket: §11 asks for a structure that cannot be wrong, so the assertion
 * is that each node states the real parent from `(round + 1, slot >> 1)`
 * and that the final states it has none.
 */

const ADMIN = { id: "a", username: "op", display_name: "Op", roles: ["admin"] };

const summary = {
  tournament_id: "t-1",
  name: "Friday Blitz",
  format: "single_elimination",
  variant: "russian_8x8",
  speed_class: "blitz",
  status: "completed",
  rated: true,
  capacity: 4,
  entrant_count: 4,
  registration_deadline: null,
  started_at: "2026-01-01T00:00:00Z",
  completed_at: "2026-01-01T01:00:00Z",
  created_at: "2026-01-01T00:00:00Z",
};

const pairing = (round: number, slot: number, matchIds: string[] = []) => ({
  round_number: round,
  slot,
  light_player_id: `p-${round}-${slot}-l`,
  dark_player_id: `p-${round}-${slot}-d`,
  light_seed: null,
  dark_seed: null,
  winner_id: null,
  advancement_reason: null,
  match_ids: matchIds,
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

function stubApi(detail?: Record<string, unknown>) {
  const queries: string[] = [];
  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    if (url.endsWith("/admin/me")) return Promise.resolve(json({ data: ADMIN }));
    if (url.includes("/admin/tournaments/")) {
      return Promise.resolve(json({ data: detail ?? {} }));
    }
    if (url.includes("/admin/tournaments")) {
      queries.push(url);
      return Promise.resolve(json({ data: { items: [summary], next_cursor: null } }));
    }
    return Promise.resolve(json({}, 404));
  });
  return queries;
}

function renderAt(path: string) {
  const router = createAdminRouter(createMemoryHistory({ initialEntries: [path] }));
  render(<App router={router} />);
  return router;
}

afterEach(() => {
  vi.unstubAllGlobals();
  accessToken.clear();
});

it("lists tournaments with status, format and entrant count", async () => {
  stubApi();
  renderAt("/tournaments");

  const table = await screen.findByRole("table");
  expect(within(table).getByText("Friday Blitz")).toBeInTheDocument();
  expect(within(table).getByText("completed")).toBeInTheDocument();
  expect(within(table).getByText("4 / 4")).toBeInTheDocument();
});

it("sends a filter as a typed parameter and keeps it in the URL", async () => {
  const queries = stubApi();
  const router = renderAt("/tournaments?status=in_progress");

  await waitFor(() =>
    expect(queries.some((url) => url.includes("status=in_progress"))).toBe(true),
  );
  expect(router.state.location.search).toMatchObject({ status: "in_progress" });
});

it("states each bracket node's real parent, and the final's absence of one", async () => {
  // §11 — correctness over decoration. The parent is (round + 1, slot >> 1),
  // the domain's own arithmetic. Two round-1 nodes both feed round 2 slot 0;
  // the round-2 node is the final and feeds nothing. A drawn tree could get
  // this wrong in CSS; text derived from the data cannot.
  stubApi({
    tournament: summary,
    entrants: [],
    rounds: [
      {
        round_number: 1,
        status: "completed",
        pairing_count: 2,
        published_at: null,
        started_at: null,
        completed_at: null,
      },
      {
        round_number: 2,
        status: "completed",
        pairing_count: 1,
        published_at: null,
        started_at: null,
        completed_at: null,
      },
    ],
    pairings: [pairing(1, 0, ["m-1"]), pairing(1, 1), pairing(2, 0)],
    standings: [],
  });
  renderAt("/tournaments/t-1");

  await screen.findByText(/Friday Blitz/);

  // Both round-1 slots converge on round 2, slot 0.
  const feeds = await screen.findAllByText(/round 2, slot 0|2-raund, 0-slot|раунд 2, слот 0/i);
  expect(feeds).toHaveLength(2);

  // The final says it has no further round rather than pointing nowhere.
  expect(
    screen.getByText(/final — no further round|final — keyingi raund yo'q|финал — дальше нет/i),
  ).toBeInTheDocument();

  // And its match links through to the Matches console.
  expect(
    screen.getByRole("link", { name: /open match|o'yinni ochish|открыть партию/i }),
  ).toBeTruthy();
});
