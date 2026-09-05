import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { App } from "@/app/App";
import { createAdminRouter } from "@/app/router";
import { accessToken } from "@/app/session-store";

/**
 * The visual contract — A64-027A.2 §35.
 *
 * A visual redesign is mostly CSS, and CSS is the wrong thing to assert:
 * pinning a padding value produces a test that fails on every improvement
 * and catches no defect. What is worth pinning is the small set of
 * decisions that a later refactor could silently undo and nobody would
 * notice from a screenshot review.
 *
 * Each test below corresponds to one mutation named in §35.
 */

const ADMIN = { id: "a", username: "op", display_name: "Operator", roles: ["admin"] };

const dashboard = (overrides: Record<string, unknown> = {}) => ({
  accounts: { registered_last_day: 3, registered_last_week: 11 },
  matches: { active: 4, awaiting_acceptance: 2 },
  tournaments: { registration_open: 1, in_progress: 2 },
  attention: { restrictions_in_force: 0, push_deliveries_retry_exhausted: 0 },
  recent_activity: [],
  generated_at: "2026-09-05T09:30:00Z",
  ...overrides,
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

function stubApi(body: Record<string, unknown> = dashboard()) {
  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    if (url.endsWith("/admin/me")) return Promise.resolve(json({ data: ADMIN }));
    if (url.includes("/admin/dashboard")) return Promise.resolve(json({ data: body }));
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

/** §35 A — navigation keeps its words. */
it("names every destination in text, not by icon alone", async () => {
  stubApi();
  renderAt("/");

  const nav = await screen.findByRole("navigation", {
    name: /admin sections|bo'limlari|разделы админки/i,
  });
  const links = within(nav).getAllByRole("link");
  expect(links.length).toBeGreaterThanOrEqual(8);

  // An icon is decoration beside a label. A rail that dropped its labels
  // would still render eight links — each with an empty accessible name,
  // which is what this asserts against.
  for (const link of links) {
    expect(link.textContent?.trim()).not.toBe("");
    expect(link).toHaveAccessibleName(/\S/);
  }
});

/** §35 A — and the active one is announced, not merely tinted. */
it("marks the active destination for a screen reader, not only with colour", async () => {
  stubApi();
  renderAt("/moderation");

  const nav = await screen.findByRole("navigation", {
    name: /admin sections|bo'limlari|разделы админки/i,
  });
  const active = within(nav)
    .getAllByRole("link")
    .filter((link) => link.getAttribute("aria-current") === "page");
  expect(active).toHaveLength(1);
});

/** §35 E — an empty attention list is an answer, not an absence. */
it("says all clear rather than rendering nothing when nothing needs attention", async () => {
  stubApi();
  renderAt("/");

  const heading = await screen.findByRole("heading", {
    name: /E'tibor talab qiladi|Требует внимания|Needs attention/,
  });
  const panel = heading.closest("section");
  expect(panel).not.toBeNull();

  // The section is present, it announces itself, and it says so in words.
  const status = within(panel as HTMLElement).getByRole("status");
  expect(status).toHaveTextContent(/Hammasi joyida|Всё в порядке|All clear/);
});

/** §35 C — a figure the server did not send is never drawn as zero. */
it("renders a genuine zero as zero and does not invent one", async () => {
  stubApi(dashboard({ matches: { active: 0, awaiting_acceptance: 0 } }));
  renderAt("/");

  const inPlay = await screen.findByText(/^Jarayonda$|^Идут$|^In play$/);
  expect(inPlay.closest(".stat")).toHaveTextContent("0");
});

/**
 * §35 D — no trend anywhere.
 *
 * The platform computes no comparable previous period, so a percentage
 * beside a metric would be decoration wearing the costume of a
 * measurement. `StatCard` has no `delta` prop; this asserts the rendered
 * result, which is what a future author would actually reach for.
 */
it("shows no trend percentage or direction arrow beside any metric", async () => {
  stubApi();
  renderAt("/");

  await screen.findByText(/^Jarayonda$|^Идут$|^In play$/);
  const cards = document.querySelectorAll(".stat");
  expect(cards.length).toBeGreaterThan(0);

  for (const card of cards) {
    const text = card.textContent ?? "";
    expect(text).not.toMatch(/[+-]\s?\d+(\.\d+)?\s?%/);
    expect(text).not.toMatch(/[↑↓▲▼]/);
    expect(text).not.toMatch(/vs\.? (last|previous)/i);
  }
});

/** The rail carries the platform's mark, not a monogram invented here. */
it("shows Arena64's own mark in the rail", async () => {
  stubApi();
  renderAt("/");

  const mark = await screen.findByRole("img", { name: "Arena64" });
  expect(mark.closest(".sidebar")).not.toBeNull();
});

it("gives sign-in the console's own identity and one primary action", async () => {
  // Four tasks of A64-027A redesigned the routes behind the guard and none
  // reached the page in front of it: a bare form at the top of an empty
  // page, its only action wearing the secondary treatment — A64-027A.5 §36.
  stubApi();
  renderAt("/login");

  const mark = await screen.findByRole("img", { name: "Arena64" });
  expect(mark.closest(".gate__card")).not.toBeNull();

  const submit = screen.getByRole("button", { name: /sign in/i });
  expect(submit).toHaveClass("primary");
});
