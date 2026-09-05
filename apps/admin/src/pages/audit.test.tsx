import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { App } from "@/app/App";
import { createAdminRouter } from "@/app/router";
import { accessToken } from "@/app/session-store";

/**
 * The Audit console — A64-024.8.
 *
 * Four tests through the real router. Each asserts something that would be
 * wrong in a way an operator could not detect: an operator action rendered
 * as if a person did it, a subject linked to a route that does not exist, a
 * filter pair sent apart, and an unknown action rendered as an empty cell.
 */

const ADMIN = { id: "a", username: "op", display_name: "Op", roles: ["admin"] };

const entry = (overrides: Record<string, unknown> = {}) => ({
  id: "e-1",
  action: "admin.role.grant",
  outcome: "succeeded",
  actor: { type: "administrator", account_id: "u-1", username: "sanjar" },
  subject: { type: "account", ref: "u-2", username: "aziza" },
  before: {},
  after: { role: "admin" },
  correlation_id: "corr-1",
  created_at: "2026-08-09T12:00:00Z",
  ...overrides,
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
    if (url.includes("/admin/audit")) {
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

it("composes the sentence from facts and links both accounts", async () => {
  stubApi([entry()]);
  renderAt("/audit");

  const table = await screen.findByRole("table");
  // The names are links to their own pages — an incident review starts at an
  // entry and continues at the account it names.
  expect(within(table).getByRole("link", { name: "sanjar" })).toHaveAttribute(
    "href",
    "/users/u-1",
  );
  expect(within(table).getByRole("link", { name: "aziza" })).toHaveAttribute(
    "href",
    "/users/u-2",
  );
});

it("renders an operator action as a process, never as an account", async () => {
  // The deployment's first grant is made from a shell with no administrator
  // behind it. Rendering a name there — any name — would be a fabricated
  // attribution in the one record that must not carry one.
  stubApi([
    entry({
      id: "e-2",
      actor: { type: "operator", account_id: null, username: null },
    }),
  ]);
  renderAt("/audit");

  const table = await screen.findByRole("table");
  expect(within(table).getByText(/operator/i)).toBeInTheDocument();
  expect(within(table).queryByRole("link", { name: "sanjar" })).not.toBeInTheDocument();
});

it("renders an unknown subject type as text rather than a broken link", async () => {
  // The trail outlives the console reading it. A subject type this build
  // does not know must not become a route that does not exist — a dead link
  // in an incident review is worse than plain text.
  stubApi([
    entry({
      id: "e-3",
      action: "moderation.note.add",
      subject: { type: "queue_ticket", ref: "q-9", username: null },
    }),
  ]);
  renderAt("/audit");

  const table = await screen.findByRole("table");
  // A64-027A.3 translates known subject types. An unknown one keeps its
  // identifier — never a translation key, which is what a bare `t()` on a
  // built key would have printed and which is worse than the enum it
  // replaced.
  expect(within(table).getByText("queue_ticket")).toBeInTheDocument();
  expect(within(table).getByText("q-9")).toBeInTheDocument();
  expect(within(table).queryByRole("link", { name: /q-9/ })).not.toBeInTheDocument();
  // And the unphraseable action keeps its identifier instead of vanishing.
  expect(within(table).getByText("moderation.note.add")).toBeInTheDocument();

  // §44 A — no raw translation key anywhere on the page.
  expect(document.body.textContent ?? "").not.toMatch(
    /\b(vocab|audit|matches|tournaments)\.[a-zA-Z]+\./,
  );
});

it("offers the action filter as phrases, never as identifiers", async () => {
  // The filter listed the raw keys of the very map that translates them, so
  // the table read "granted the admin role" while the control above it read
  // `admin.role.grant` — A64-027A.5 §28. One vocabulary, both places.
  stubApi([entry()]);
  renderAt("/audit");

  const filter = await screen.findByLabelText(/action/i);
  const options = within(filter).getAllByRole("option");
  expect(options.length).toBeGreaterThan(1);
  for (const option of options) {
    expect(option.textContent ?? "").not.toMatch(/^[a-z]+(\.[a-z_]+)+$/);
  }
  expect(within(filter).getByRole("option", { name: "granted the admin role" })).toBeInTheDocument();
});

it("sends the subject filter as a type and ref together", async () => {
  // The server refuses a bare `subject_ref`, because the index it needs
  // leads with the type. Sending one alone would be a `400` an operator
  // would read as "the audit log is broken".
  const queries = stubApi([entry()]);
  const router = renderAt("/audit?subject=u-2");

  await waitFor(() =>
    expect(queries.some((url) => url.includes("subject_ref=u-2"))).toBe(true),
  );
  expect(queries.at(-1)).toContain("subject_type=account");
  expect(router.state.location.search).toMatchObject({ subject: "u-2" });
});

it("says a filter is applied and can clear it", async () => {
  // The failure this prevents is a silent one: a filtered audit log returns
  // no rows, the operator reads "nothing happened", and the entry they were
  // looking for is one filter away — A64-027A.5 §36.
  const queries = stubApi([]);
  const router = renderAt("/audit?subject=u-2");

  const clear = await screen.findByRole("button", { name: /clear/i });
  expect(clear).toHaveTextContent("1");

  await userEvent.click(clear);
  await waitFor(() => {
    expect(router.state.location.search).toEqual({});
  });
  // And the next request actually drops the filter, rather than only the URL.
  await waitFor(() => {
    expect(queries.at(-1)).not.toContain("subject_ref=");
  });
});
