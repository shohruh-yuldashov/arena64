import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { App } from "@/app/App";
import { createAdminRouter } from "@/app/router";
import { accessToken } from "@/app/session-store";

/**
 * The management pages' contract — A64-027A.3 §44.
 *
 * Not pixel assertions. What is pinned here is the handful of decisions a
 * later change could undo without anybody noticing in a screenshot review:
 * that a database value never reaches the screen, that an empty result and
 * a failed request are different screens, and that a filtered listing says
 * it is filtered.
 */

const ADMIN = { id: "a", username: "op", display_name: "Operator", roles: ["admin"] };
const uid = (i: number) => `0000000${String(i)}-1111-4111-8111-11111111111${String(i)}`;

const seat = (i: number, username: string, side: string) => ({
  player_id: uid(i),
  username,
  display_name: username,
  side,
});

const match = (overrides: Record<string, unknown> = {}) => ({
  match_id: "m1",
  status: "completed",
  variant: "russian_8x8",
  rated: true,
  origin: "queue",
  light: seat(1, "alice", "light"),
  dark: seat(2, "bob", "dark"),
  outcome: "win",
  winner: "light",
  termination_reason: "resignation",
  speed_class: "blitz",
  ply_number: 42,
  created_at: "2026-09-04T10:00:00Z",
  ended_at: "2026-09-04T10:22:00Z",
  ...overrides,
});

const tournament = (overrides: Record<string, unknown> = {}) => ({
  tournament_id: "t1",
  name: "Kuzgi kubok",
  format: "swiss",
  variant: "russian_8x8",
  speed_class: "blitz",
  status: "registration_open",
  rated: true,
  capacity: 64,
  entrant_count: 41,
  registration_deadline: null,
  started_at: null,
  completed_at: null,
  created_at: "2026-09-01T10:00:00Z",
  ...overrides,
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

function stubApi(items: unknown[], status = 200) {
  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    if (url.endsWith("/admin/me")) return Promise.resolve(json({ data: ADMIN }));
    if (url.includes("/admin/")) {
      if (status !== 200) return Promise.resolve(json({}, status));
      return Promise.resolve(json({ data: { items, next_cursor: null } }));
    }
    return Promise.resolve(json({}, 404));
  });
}

function renderAt(path: string) {
  render(<App router={createAdminRouter(createMemoryHistory({ initialEntries: [path] }))} />);
}

afterEach(() => {
  vi.unstubAllGlobals();
  accessToken.clear();
});

/**
 * §44 A — no database value on screen.
 *
 * The check is a *shape*, not a list: any lower-case token with an
 * underscore is an enum member that escaped, and any dotted word is a
 * translation key that did. Both are things an administrator would have to
 * decode, and both have reached this console before.
 */
const RAW_ENUM = /\b[a-z]+_[a-z_]+\b/;
const RAW_KEY = /\b(vocab|matches|tournaments|users|audit|moderation)\.[a-zA-Z]+\b/;

it("shows no database value or translation key on the matches listing", async () => {
  stubApi([
    match(),
    match({ match_id: "m2", status: "pending_acceptance", origin: "challenge" }),
  ]);
  renderAt("/matches");

  const table = await screen.findByRole("table");
  const text = table.textContent ?? "";
  expect(text).not.toMatch(RAW_ENUM);
  expect(text).not.toMatch(RAW_KEY);
  // And the words that replaced them are actually there.
  expect(within(table).getAllByText(/From queue|Navbatdan|Из очереди/).length).toBeGreaterThan(
    0,
  );
});

it("shows no database value or translation key on the tournaments listing", async () => {
  stubApi([tournament(), tournament({ tournament_id: "t2", status: "in_progress" })]);
  renderAt("/tournaments");

  const table = await screen.findByRole("table");
  const text = table.textContent ?? "";
  expect(text).not.toMatch(RAW_ENUM);
  expect(text).not.toMatch(RAW_KEY);
});

/** §44 B — an empty result is a sentence, never a blank region. */
it("says a filtered listing found nothing rather than rendering an empty page", async () => {
  stubApi([]);
  renderAt("/matches?status=expired");

  const empty = await screen.findByRole("heading", {
    name: /topilmadi|не найден|No matches/i,
  });
  expect(empty).toBeInTheDocument();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
});

/** §44 C — a failed request is not an empty result. */
it("distinguishes a failed request from an empty one", async () => {
  stubApi([], 500);
  renderAt("/matches");

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/\S/);
  // The empty state must NOT be what a failure renders: they mean opposite
  // things and an operator would stop investigating.
  expect(screen.queryByRole("heading", { name: /topilmadi|не найден|No matches/i })).toBeNull();
});

/** §44 G — an applied filter says so, and offers a way out. */
it("tells the operator a filter is applied", async () => {
  stubApi([match()]);
  renderAt("/matches?status=completed&rated=true");

  const clear = await screen.findByRole("button", {
    name: /Filtrlarni tozalash|Сбросить фильтры|Clear filters/i,
  });
  // Two filters are in the URL, and the control names how many.
  expect(clear).toHaveTextContent("2");
});

it("offers no clear control when nothing is filtered", async () => {
  stubApi([match()]);
  renderAt("/matches");

  await screen.findByRole("table");
  expect(
    screen.queryByRole("button", {
      name: /Filtrlarni tozalash|Сбросить фильтры|Clear filters/i,
    }),
  ).not.toBeInTheDocument();
});

/** §44 G — the entrant bar is the server's ratio, never an invented total. */
it("draws entrant progress from the two real numbers", async () => {
  stubApi([tournament({ entrant_count: 41, capacity: 64 })]);
  renderAt("/tournaments");

  const table = await screen.findByRole("table");
  expect(within(table).getByText("41 / 64")).toBeInTheDocument();

  const fill = table.querySelector(".entrants__fill");
  expect(fill).not.toBeNull();
  // 41/64 = 64.06%, and the element carries no other width source.
  expect((fill as HTMLElement).style.inlineSize).toMatch(/^64\.06/);
});

/** A capacity of zero is not a full tournament. */
it("draws no entrant progress when capacity is zero", async () => {
  stubApi([tournament({ entrant_count: 0, capacity: 0 })]);
  renderAt("/tournaments");

  const table = await screen.findByRole("table");
  const fill = table.querySelector(".entrants__fill");
  expect((fill as HTMLElement).style.inlineSize).toBe("0%");
});
