import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { App } from "@/app/App";
import { createAdminRouter } from "@/app/router";
import { accessToken } from "@/app/session-store";

/**
 * The operator dashboard — A64-024.9 §27.
 *
 * Five tests through the real router. Each asserts something an operator
 * could not detect by looking: that zero reads as zero, that a failed
 * refresh does not silently become zeros, that every figure links somewhere
 * that can act on it, and that no card carries an action of its own.
 */

const ADMIN = { id: "a", username: "op", display_name: "Op", roles: ["admin"] };

const dashboard = (overrides: Record<string, unknown> = {}) => ({
  accounts: { registered_last_day: 3, registered_last_week: 11 },
  matches: { active: 4, awaiting_acceptance: 2 },
  tournaments: { registration_open: 1, in_progress: 2 },
  attention: { restrictions_in_force: 5, push_deliveries_retry_exhausted: 7 },
  recent_activity: [
    {
      id: "e-1",
      action: "admin.sanction.apply",
      outcome: "succeeded",
      actor_type: "administrator",
      actor_id: "u-1",
      actor_username: "sanjar",
      subject_type: "account",
      subject_ref: "u-2",
      created_at: "2026-08-09T12:00:00Z",
    },
    {
      id: "e-2",
      action: "some.future.action",
      outcome: "succeeded",
      actor_type: "operator",
      actor_id: null,
      actor_username: null,
      subject_type: "account",
      subject_ref: "u-3",
      created_at: "2026-08-09T11:00:00Z",
    },
  ],
  generated_at: "2026-08-09T12:05:00Z",
  ...overrides,
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

interface Stub {
  calls: number;
  payloads: unknown[];
  statuses: number[];
}

function stubApi(payloads: unknown[], statuses: number[] = []): Stub {
  const stub: Stub = { calls: 0, payloads, statuses };

  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    if (url.endsWith("/admin/me")) return Promise.resolve(json({ data: ADMIN }));
    if (url.includes("/admin/dashboard")) {
      const index = Math.min(stub.calls, payloads.length - 1);
      const status = stub.statuses[stub.calls] ?? 200;
      stub.calls += 1;
      return Promise.resolve(json({ data: payloads[index] }, status));
    }
    return Promise.resolve(json({}, 404));
  });
  return stub;
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

it("renders every fact in one request and links each into a console", async () => {
  // Six numbers from one round trip. And every one of them is a link: a
  // figure an operator cannot click through to is a figure they have to go
  // looking for, which is what a dashboard exists to save them.
  const stub = stubApi([dashboard()]);
  const person = userEvent.setup();
  const router = renderAt("/");

  await screen.findByRole("heading", { level: 3, name: /Umumiy holat|Обзор|Overview/ });
  expect(stub.calls).toBe(1);

  expect(screen.getByText("4")).toBeInTheDocument();
  expect(screen.getByText("11")).toBeInTheDocument();

  // Scoped to the cards: the sidebar and the shortcut list name the same
  // sections, and an unscoped query would match whichever came first.
  const overview = (
    await screen.findByRole("heading", { level: 3, name: /Umumiy holat|Обзор|Overview/ })
  ).closest("section") as HTMLElement;
  expect(
    within(overview).getByRole("link", { name: /O'yinlar|Партии|^Matches$/ }),
  ).toHaveAttribute("href", "/matches?status=active");

  const attention = screen
    .getByRole("heading", {
      level: 3,
      name: /E'tibor talab qiladi|Требует внимания|Needs attention/,
    })
    .closest("section") as HTMLElement;
  expect(
    within(attention).getByRole("link", {
      name: /Amaldagi cheklovlar|Действующие|Restrictions in force/,
    }),
  ).toHaveAttribute("href", "/moderation");
  // Asserted by **navigating** rather than by reading the href: the router
  // quotes a search value that would otherwise parse back as a boolean, so
  // the URL reads `failed=%22true%22` while the destination receives the
  // string `"true"` its `validateSearch` declares. What matters is that the
  // page understands the parameter (§21), not how it is spelled on the way.
  await person.click(
    within(attention).getByRole("link", { name: /tugagan push|исчерпанными|out of retries/ }),
  );
  await waitFor(() => expect(router.state.location.pathname).toBe("/notifications"));
  expect(router.state.location.search).toMatchObject({ failed: "true" });
});

it("renders zero as zero and says so rather than hiding the section", async () => {
  // `0` must mean zero. A dashboard that dropped the attention list when it
  // was empty would leave an operator unable to tell "nothing to do" from
  // "that part did not load".
  stubApi([
    dashboard({
      accounts: { registered_last_day: 0, registered_last_week: 0 },
      matches: { active: 0, awaiting_acceptance: 0 },
      attention: { restrictions_in_force: 0, push_deliveries_retry_exhausted: 0 },
      recent_activity: [],
    }),
  ]);
  renderAt("/");

  await screen.findByRole("heading", { level: 3, name: /Umumiy holat|Обзор|Overview/ });
  expect(screen.getAllByText("0").length).toBeGreaterThan(0);
  expect(screen.getByText(/Hammasi joyida|Всё в порядке|All clear/)).toBeInTheDocument();
  expect(
    screen.getByText(
      /qayd etilmagan|Действий администраторов пока нет|No administrative actions/,
    ),
  ).toBeInTheDocument();
});

it("keeps the numbers on screen when a refresh fails", async () => {
  // The one lie this page must not tell. The figures were true when they
  // were fetched and the page says when that was; replacing them with zeros
  // because a later request failed would invent an all-clear.
  const stub = stubApi([dashboard(), { detail: "no" }], [200, 500]);
  const person = userEvent.setup();
  renderAt("/");

  await screen.findByRole("heading", { level: 3, name: /Umumiy holat|Обзор|Overview/ });
  await person.click(screen.getByRole("button", { name: /Yangilash|Обновить|Refresh/ }));

  await waitFor(() => expect(stub.calls).toBe(2));
  await screen.findByRole("alert");
  // Still the known-good numbers, not zeros.
  expect(screen.getByText("4")).toBeInTheDocument();
  expect(screen.getByText("11")).toBeInTheDocument();
});

it("shows the newest figures after a successful refresh", async () => {
  const stub = stubApi([
    dashboard(),
    dashboard({ matches: { active: 9, awaiting_acceptance: 0 } }),
  ]);
  const person = userEvent.setup();
  renderAt("/");

  await screen.findByRole("heading", { level: 3, name: /Umumiy holat|Обзор|Overview/ });
  expect(screen.getByText("4")).toBeInTheDocument();

  await person.click(screen.getByRole("button", { name: /Yangilash|Обновить|Refresh/ }));

  await waitFor(() => expect(stub.calls).toBe(2));
  await screen.findByText("9");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("carries no action of its own, and names an operator entry as a process", async () => {
  // §15 — every card links to the console that owns the work. A retry beside
  // a failure count is one clicked without reading which failure it was.
  //
  // And an action this build cannot phrase keeps its identifier rather than
  // rendering blank: the trail outlives the console reading it.
  stubApi([dashboard()]);
  renderAt("/");

  const activity = await screen.findByRole("heading", {
    level: 3,
    name: /So'nggi admin amallari|Последние действия|Recent administrative activity/,
  });
  const section = activity.closest("section");
  expect(section).not.toBeNull();

  expect(within(section as HTMLElement).getByText(/some\.future\.action/)).toBeInTheDocument();
  expect(
    within(section as HTMLElement).getByText(
      /Operator \(konsol\)|Оператор|Operator \(console\)/,
    ),
  ).toBeInTheDocument();

  // The only button the dashboard itself renders is the read-only refresh.
  // Scoped to `<main>`, because the shell's sign-out button is not this
  // page's and would make the assertion about the layout instead.
  const main = document.querySelector("main") as HTMLElement;
  const buttons = within(main).getAllByRole("button");
  expect(buttons).toHaveLength(1);
  expect(buttons[0]).toHaveAccessibleName(/Yangilash|Обновить|Refresh/);
});
