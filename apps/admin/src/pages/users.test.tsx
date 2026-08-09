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
  expect(within(rootRow).getByText(/unverified|tasdiqlanmagan|не подтверждён/i)).toBeInTheDocument();
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
