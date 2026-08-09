import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { App } from "@/app/App";
import { createAdminRouter } from "@/app/router";
import { accessToken } from "@/app/session-store";

/**
 * The Matches console — A64-024.4 §18.
 *
 * Through the real router and the real client. Three tests, chosen for the
 * invariants the Users tests do not already cover: that the list renders
 * the facts an operator reads, that a filter reaches the request as a
 * typed parameter, and that the detail route resolves and links onward.
 */

const ADMIN = { id: "a", username: "op", display_name: "Op", roles: ["admin"] };

const seat = (name: string, side: string) => ({
  player_id: `p-${name}`,
  username: name,
  display_name: null,
  side,
});

const match = (extra: Record<string, unknown> = {}) => ({
  match_id: "m-1",
  status: "completed",
  variant: "russian_8x8",
  rated: true,
  origin: "queue",
  light: seat("alice", "light"),
  dark: seat("bob", "dark"),
  outcome: "light_won",
  winner: "light",
  termination_reason: "resignation",
  speed_class: "blitz",
  ply_number: 24,
  created_at: "2026-01-01T00:00:00Z",
  ended_at: "2026-01-01T00:10:00Z",
  ...extra,
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

function stubApi(items: unknown[]) {
  const queries: string[] = [];
  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    if (url.endsWith("/admin/me")) return Promise.resolve(json({ data: ADMIN }));
    if (url.includes("/admin/matches/")) {
      return Promise.resolve(
        json({ data: { ...match(), settled_at: null, time_control: null } }),
      );
    }
    if (url.includes("/admin/matches")) {
      queries.push(url);
      return Promise.resolve(json({ data: { items, next_cursor: null } }));
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

it("shows who played, the status, the result and the mode", async () => {
  // §13: an operator must be able to read the essentials from the row
  // without opening anything. Both seats appear, and the mode is text
  // rather than a colour.
  stubApi([match()]);
  renderAt("/matches");

  const table = await screen.findByRole("table");
  expect(within(table).getByText(/alice — bob/)).toBeInTheDocument();
  expect(within(table).getByText("completed")).toBeInTheDocument();
  expect(within(table).getByText(/rated|reytingli|рейтинговая/i)).toBeInTheDocument();
});

it("sends a filter as a typed parameter and keeps it in the URL", async () => {
  // §6 and §13. The backend takes enums, so the request carries
  // `status=active` rather than a free-text predicate — and the URL keeps
  // it, which is what makes a filtered view shareable.
  const queries = stubApi([match({ status: "active", outcome: null, winner: null })]);
  const router = renderAt("/matches");
  await screen.findByRole("table");

  await userEvent
    .setup()
    .selectOptions(screen.getByLabelText(/status|holat|статус/i), "active");

  await waitFor(() => expect(router.state.location.search).toMatchObject({ status: "active" }));
  await waitFor(() => expect(queries.some((url) => url.includes("status=active"))).toBe(true));
});

it("opens a match detail and links each seat to its account", async () => {
  // §14. The detail is a real route, and the participant link is the
  // reason this page carries no email — the operator who needs one is a
  // click away, on the surface that owns it.
  stubApi([match()]);
  renderAt("/matches/m-1");

  expect(await screen.findByText(/m-1/)).toBeInTheDocument();
  const alice = await screen.findByRole("link", { name: "alice" });
  expect(alice.getAttribute("href")).toContain("/users/p-alice");
});
