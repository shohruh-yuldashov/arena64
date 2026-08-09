import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { App } from "@/app/App";
import { createAdminRouter } from "@/app/router";
import { accessToken } from "@/app/session-store";

/**
 * The Users console — A64-024.3 §19.
 *
 * Through the real router, the real guard and the real client. The rows
 * come from a stubbed `fetch`, so what is asserted is what an operator
 * sees: the table, the search round trip, and the states that are not a
 * table.
 */

const ADMIN = { id: "a", username: "op", display_name: "Op", roles: ["admin"] };

const user = (username: string, extra: Record<string, unknown> = {}) => ({
  id: `id-${username}`,
  username,
  display_name: null,
  email: `${username}@example.com`,
  is_active: true,
  is_verified: true,
  created_at: "2026-01-01T00:00:00Z",
  is_admin: false,
  ...extra,
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

/** Records every `/admin/users` query so a test can assert the round trip. */
function stubApi(page: (query: string) => unknown) {
  const queries: string[] = [];
  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    if (url.endsWith("/admin/me")) return Promise.resolve(json({ data: ADMIN }));
    if (url.includes("/admin/users")) {
      queries.push(url);
      const result = page(url);
      return Promise.resolve(
        result === null ? json({}, 500) : json({ data: { items: result, next_cursor: null } }),
      );
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

it("lists accounts with their status, verification and role", async () => {
  // §19.1 and §12. Every column an operator reads is asserted, including
  // the role — which is the one field composed from a second module and so
  // the one most likely to be dropped by a refactor.
  stubApi(() => [user("alice"), user("root", { is_admin: true, is_verified: false })]);
  renderAt("/users");

  const table = await screen.findByRole("table");
  expect(within(table).getByText("alice@example.com")).toBeInTheDocument();

  const rootRow = within(table).getByText("root").closest("tr")!;
  expect(within(rootRow).getByText(/admin/i)).toBeInTheDocument();
  // Status is text, never colour alone — §17.
  expect(
    within(rootRow).getByText(/unverified|tasdiqlanmagan|не подтверждён/i),
  ).toBeInTheDocument();
});

it("puts the search term in the URL and asks the server for it", async () => {
  // §14. The query lives in the router, so a filtered search is a link an
  // operator can send — and the request carries the term rather than the
  // page filtering rows it already has.
  const queries = stubApi((url) => (url.includes("q=ali") ? [user("alice")] : [user("bob")]));
  const router = renderAt("/users");
  await screen.findByRole("table");

  await userEvent.setup().type(screen.getByLabelText(/search|qidirish|поиск/i), "ali");

  await waitFor(() => expect(router.state.location.search).toMatchObject({ q: "ali" }));
  await waitFor(() => expect(queries.some((url) => url.includes("q=ali"))).toBe(true));
});

it("says so plainly when there is nothing to show or the read fails", async () => {
  // §19.5. Two states that are not a table, and both have to be
  // distinguishable from "still loading" — an empty result rendered as a
  // blank page is indistinguishable from a broken one.
  stubApi(() => []);
  renderAt("/users");
  expect(await screen.findByText(/no users found|topilmadi|не найдены/i)).toBeInTheDocument();

  vi.unstubAllGlobals();
  accessToken.clear();
  stubApi(() => null);
  renderAt("/users");
  expect(await screen.findByRole("alert")).toBeInTheDocument();
});

// --- pagination — A64-024.3H -------------------------------------------------

/** A stub whose second page continues the first, as the server's would. */
function stubPages(pages: { items: unknown[]; next_cursor: string | null }[]) {
  const queries: string[] = [];
  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    if (url.endsWith("/admin/me")) return Promise.resolve(json({ data: ADMIN }));
    if (url.includes("/admin/users")) {
      queries.push(url);
      const cursor = new URL(url, "http://x").searchParams.get("cursor");
      // The first page has no cursor; any cursor asks for the next one.
      const page = cursor === null ? pages[0] : pages[1];
      if (page === undefined) return Promise.resolve(json({}, 500));
      return Promise.resolve(json({ data: page }));
    }
    return Promise.resolve(json({}, 404));
  });
  return queries;
}

it("appends the next page without duplicating or replacing rows", async () => {
  // The whole point of "Load more": the rows already on screen stay, and
  // the new ones join them in the server's order. Deduplicated on the way
  // in — the keyset is total, so a repeat should be impossible, and the
  // cost of being wrong is a React key collision.
  const queries = stubPages([
    { items: [user("alice"), user("bob")], next_cursor: "c1" },
    // `bob` repeated deliberately: the guard must drop it.
    { items: [user("bob"), user("carol")], next_cursor: null },
  ]);
  renderAt("/users");
  await screen.findByRole("table");

  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: /load more|ko'proq|показать ещё/i }));

  await waitFor(() => expect(screen.getAllByText(/carol/).length).toBeGreaterThan(0));
  const table = screen.getByRole("table");
  // Three distinct rows, not four — and the first page is still there.
  expect(within(table).getAllByRole("row")).toHaveLength(4); // header + 3
  expect(within(table).getByText("alice")).toBeInTheDocument();
  expect(queries.some((url) => url.includes("cursor=c1"))).toBe(true);

  // The last page carries no cursor, so the control goes rather than
  // sitting there disabled and promising something.
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: /load more|ko'proq|показать ещё/i }),
    ).toBeNull(),
  );
});

it("keeps the loaded rows when a further page fails", async () => {
  // A transient failure must not cost an operator their place in the list.
  stubPages([{ items: [user("alice")], next_cursor: "c1" }]);
  renderAt("/users");
  await screen.findByRole("table");

  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: /load more|ko'proq|показать ещё/i }));

  expect(await screen.findByRole("alert")).toBeInTheDocument();
  expect(within(screen.getByRole("table")).getByText("alice")).toBeInTheDocument();
});

it("drops the cursor and the rows when the search changes", async () => {
  // §4: a changed query starts a new result set. Reusing a cursor would
  // ask the server to continue a list that no longer exists.
  const queries = stubPages([
    { items: [user("alice")], next_cursor: "c1" },
    { items: [user("bob")], next_cursor: null },
  ]);
  renderAt("/users");
  await screen.findByRole("table");
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: /load more|ko'proq|показать ещё/i }));
  await waitFor(() => expect(queries.some((url) => url.includes("cursor="))).toBe(true));

  queries.length = 0;
  await userEvent.setup().type(screen.getByLabelText(/search|qidirish|поиск/i), "zz");

  // The next request for the new term carries no cursor at all.
  await waitFor(() => expect(queries.some((url) => url.includes("q=zz"))).toBe(true));
  expect(
    queries.filter((url) => url.includes("q=zz")).every((url) => !url.includes("cursor=")),
  ).toBe(true);
});
