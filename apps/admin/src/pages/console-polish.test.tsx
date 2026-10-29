import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, expect, it, vi } from "vitest";

import { App } from "@/app/App";
import { createAdminRouter } from "@/app/router";
import { accessToken } from "@/app/session-store";

/**
 * The console's polish pass — A64-031.G, `specs/admin.md` §15.
 *
 * Each test is a defect found by using the console rather than reading it,
 * and each fails against the code before that pass. What jsdom cannot see —
 * a section hidden by the stylesheet, a row rendered twice on a phone — was
 * measured in a real browser instead, and §15 says which is which.
 *
 * The locale is pinned to English so the assertions can name the words an
 * operator reads; the other two locales are held to the same keys by
 * `locales.test.ts`.
 */

const ADMIN = { id: "a", username: "op", display_name: "Operator", roles: ["admin"] };
const uid = (i: number) => `0000000${String(i)}-1111-4111-8111-11111111111${String(i)}`;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

type Handler = (url: string, init?: RequestInit) => Promise<Response> | undefined;

/** Signs in, then hands every other request to `handle`. */
function stubApi(handle: Handler) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    if (url.endsWith("/admin/me")) return Promise.resolve(json({ data: ADMIN }));
    return handle(url, init) ?? Promise.resolve(json({}, 404));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderAt(path: string) {
  render(<App router={createAdminRouter(createMemoryHistory({ initialEntries: [path] }))} />);
}

const user = (i: number) => ({
  id: uid(i),
  username: `player${String(i)}`,
  display_name: `Player ${String(i)}`,
  email: `player${String(i)}@example.test`,
  is_active: true,
  is_verified: true,
  created_at: "2026-10-20T10:00:00Z",
  is_admin: false,
});

const userDetail = {
  ...user(1),
  admin_role_granted_at: null,
  moderation: { is_restricted: false, restriction: null },
};

const tournament = {
  tournament_id: "t1",
  name: "Autumn Cup",
  format: "single_elimination",
  variant: "russian_8x8",
  speed_class: "blitz",
  status: "completed",
  rated: true,
  capacity: 2,
  entrant_count: 2,
  registration_deadline: null,
  started_at: "2026-10-20T10:00:00Z",
  completed_at: "2026-10-20T11:00:00Z",
  created_at: "2026-10-19T10:00:00Z",
};

const tournamentDetail = {
  tournament,
  entrants: [1, 2].map((i) => ({
    player_id: uid(i),
    username: `player${String(i)}`,
    display_name: `Player ${String(i)}`,
    status: "registered",
    seed_number: i,
    registered_at: "2026-10-19T10:00:00Z",
    withdrawn_at: null,
  })),
  rounds: [
    {
      round_number: 1,
      status: "in_progress",
      pairing_count: 1,
      published_at: "2026-10-20T10:00:00Z",
      started_at: "2026-10-20T10:00:00Z",
      completed_at: null,
    },
  ],
  pairings: [
    {
      round_number: 1,
      slot: 0,
      light_player_id: uid(1),
      dark_player_id: uid(2),
      light_seed: 1,
      dark_seed: 2,
      winner_id: uid(1),
      advancement_reason: "adjudication",
      match_ids: ["m1"],
    },
  ],
  standings: [
    ["champion", 1],
    ["runner_up", 2],
  ].map(([finalStatus, rank], index) => ({
    player_id: uid(index + 1),
    username: `player${String(index + 1)}`,
    display_name: `Player ${String(index + 1)}`,
    final_rank: rank,
    seed_number: index + 1,
    elimination_round: null,
    eliminated_by_player_id: null,
    wins: 1 - index,
    losses: index,
    draws: 0,
    final_status: finalStatus,
  })),
};

/** Same shape as `management-visual.test.tsx`: an escaped enum member. */
const RAW_ENUM = /\b[a-z]+_[a-z_]+\b/;

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.open = false;
  };
});

beforeEach(() => {
  localStorage.setItem("admin.locale", "en");
});

afterEach(() => {
  vi.unstubAllGlobals();
  accessToken.clear();
  localStorage.clear();
});

it("says a page failed to load rather than leaving the operator on the last one", async () => {
  stubApi((url) => {
    if (!url.includes("/admin/users")) return undefined;
    if (url.includes("cursor=")) return Promise.resolve(json({}, 503));
    return Promise.resolve(json({ data: { items: [user(1)], next_cursor: "c2" } }));
  });
  renderAt("/users");

  await userEvent.click(await screen.findByRole("button", { name: /Next/ }));

  expect(await screen.findByRole("alert")).toHaveTextContent(/Could not load that page/);
  // The rows that were true when fetched stay on screen.
  expect(screen.getAllByText("player1@example.test").length).toBeGreaterThan(0);
});

it("shows its loading state again when Try again is pressed on a failed listing", async () => {
  let calls = 0;
  stubApi((url) => {
    if (!url.includes("/admin/users")) return undefined;
    calls += 1;
    // The first read fails; the retry is still in flight when we look.
    return calls === 1
      ? Promise.resolve(json({}, 500))
      : new Promise<Response>(() => undefined);
  });
  renderAt("/users");

  await userEvent.click(await screen.findByRole("button", { name: "Try again" }));

  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByText(/Loading/)).toBeInTheDocument();
});

it("offers a retry when a detail page fails to load, and recovers with it", async () => {
  let calls = 0;
  stubApi((url) => {
    if (!url.includes(`/admin/users/${uid(1)}`)) return undefined;
    calls += 1;
    return Promise.resolve(calls === 1 ? json({}, 500) : json({ data: userDetail }));
  });
  renderAt(`/users/${uid(1)}`);

  await userEvent.click(await screen.findByRole("button", { name: "Try again" }));

  expect(
    await screen.findByRole("heading", { level: 2, name: "Player 1" }),
  ).toBeInTheDocument();
});

it("names the standings' player column and translates every tournament value", async () => {
  stubApi((url) =>
    url.includes("/admin/tournaments/t1")
      ? Promise.resolve(json({ data: tournamentDetail }))
      : undefined,
  );
  renderAt("/tournaments/t1");

  await screen.findByRole("heading", { level: 2, name: "Autumn Cup" });
  const standings = screen.getByRole("table", { name: "Standings" });
  expect(within(standings).getByRole("columnheader", { name: "Player" })).toBeInTheDocument();
  expect(within(standings).queryByRole("columnheader", { name: "Tournament" })).toBeNull();

  const main = screen.getByRole("main").textContent ?? "";
  expect(main).not.toMatch(RAW_ENUM);
  expect(main).toMatch(/Runner-up/);
  expect(main).toMatch(/Adjudicated — higher seed/);
});

it("offers the create form's variant and speed as words, not identifiers", async () => {
  stubApi((url) =>
    url.includes("/admin/tournaments")
      ? Promise.resolve(json({ data: { items: [], next_cursor: null } }))
      : undefined,
  );
  renderAt("/tournaments");

  await userEvent.click(await screen.findByRole("button", { name: /Create tournament/ }));

  const variant = screen.getByLabelText("Variant");
  expect(within(variant).getByRole("option").textContent).toBe("Russian draughts 8×8");
  const speeds = within(screen.getByLabelText("Speed")).getAllByRole("option");
  expect(speeds.map((option) => option.textContent)).toContain("Blitz");
});

it("does not paint a confirmation red when the action takes nothing away", async () => {
  stubApi((url) =>
    url.includes("/admin/tournaments")
      ? Promise.resolve(json({ data: { items: [], next_cursor: null } }))
      : undefined,
  );
  renderAt("/tournaments");

  await userEvent.click(await screen.findByRole("button", { name: /Create tournament/ }));

  const submit = screen.getByRole("button", { name: "Create" });
  expect(submit).toHaveClass("primary");
  expect(submit).not.toHaveClass("danger");
});

it("declares the language the console is rendered in", async () => {
  localStorage.setItem("admin.locale", "ru");
  stubApi(() => undefined);
  renderAt("/login");

  await screen.findByRole("heading", { level: 1 });
  expect(document.documentElement.lang).toBe("ru");
});

it("moves focus into the navigation sheet, holds the page behind it, and gives focus back", async () => {
  stubApi((url) =>
    url.includes("/admin/users")
      ? Promise.resolve(json({ data: { items: [user(1)], next_cursor: null } }))
      : undefined,
  );
  renderAt("/users");

  const toggle = await screen.findByRole("button", { name: "Open menu" });
  await userEvent.click(toggle);

  const navigation = screen.getByRole("navigation", { name: "Admin sections" });
  expect(navigation).toContainElement(document.activeElement as HTMLElement);
  expect(toggle.closest(".workspace")).toHaveAttribute("inert");

  await userEvent.keyboard("{Escape}");

  expect(toggle).toHaveFocus();
  expect(toggle.closest(".workspace")).not.toHaveAttribute("inert");
});

it("tells an operator who submits an empty sign-in what is missing, without asking", async () => {
  const fetchMock = stubApi(() => undefined);
  renderAt("/login");

  await userEvent.click(await screen.findByRole("button", { name: "Sign in" }));

  expect(screen.getByRole("alert")).toHaveTextContent("Enter your email and password.");
  expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/login"))).toBe(false);
});

it("labels the retry on an unreachable session as a retry", async () => {
  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    return Promise.resolve(json({}, 503));
  });
  renderAt("/users");

  expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Back to the dashboard/ })).toBeNull();
});

it("tells a screen reader why a pasted recipient list was refused", async () => {
  stubApi((url) => {
    if (url.includes("/admin/broadcasts/audience")) {
      return Promise.resolve(json({ data: { audience: "all_players", size: 10 } }));
    }
    if (url.includes("/admin/")) {
      return Promise.resolve(json({ data: { items: [], next_cursor: null } }));
    }
    return undefined;
  });
  renderAt("/notifications");

  await userEvent.click(await screen.findByRole("button", { name: /Specific users/ }));
  const recipients = screen.getByLabelText("User IDs");
  await userEvent.type(recipients, "not-an-id");

  expect(recipients).toHaveAccessibleDescription(/something that is not an ID/);
});
