import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "@/app/App";
import { createAdminRouter } from "@/app/router";
import { safeRedirect } from "@/app/safe-redirect";
import { accessToken } from "@/app/session-store";

/**
 * Admin sign-in and the protected route boundary — A64-024.2 §17.
 *
 * Driven through the **real** router, the real guard and the real client,
 * with `fetch` stubbed at the browser boundary. The status codes are the
 * ones the backend actually produces: `401` from `CurrentUser`, `403` from
 * `require_admin`, and the `{ data }` envelope the platform wraps success
 * in.
 *
 * A memory history is used so a test can assert **where the console
 * navigated to** — `window.location` never moves under one, so asserting
 * against it would pass for every route.
 */

const SESSION = {
  id: "019fd1c7-5178-7a94-8076-4eeece03a8f4",
  username: "operator",
  display_name: "Operator",
  roles: ["admin"],
};

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

/**
 * A backend, as far as this app can tell.
 *
 * `admin` decides what `/admin/me` answers, so a test can revoke a role
 * mid-session by flipping it — which is the only way to exercise
 * A64-024.1's zero-staleness property from the client's side.
 */
function stubApi(options: { signedIn?: boolean; admin?: boolean } = {}) {
  const state = { signedIn: options.signedIn ?? false, admin: options.admin ?? true };
  const calls: string[] = [];

  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push(url);

    if (url.endsWith("/auth/browser/login")) {
      const body = JSON.parse(String(init?.body ?? "{}")) as { password?: string };
      if (body.password !== "correct") return Promise.resolve(json({}, 401));
      state.signedIn = true;
      return Promise.resolve(json({ data: { access_token: "access-1" } }));
    }

    if (url.endsWith("/auth/browser/refresh")) {
      return Promise.resolve(
        state.signedIn ? json({ data: { access_token: "access-1" } }) : json({}, 401),
      );
    }

    if (url.endsWith("/auth/browser/logout")) {
      state.signedIn = false;
      return Promise.resolve(json({}));
    }

    if (url.endsWith("/admin/me")) {
      if (!state.signedIn) return Promise.resolve(json({}, 401));
      if (!state.admin) return Promise.resolve(json({}, 403));
      return Promise.resolve(json({ data: SESSION }));
    }

    return Promise.resolve(json({}, 404));
  });

  return { state, calls };
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

/**
 * The sidebar, by its accessible name.
 *
 * A64-027A gave the shell a second navigation landmark — the breadcrumb in
 * the toolbar — so "the navigation" is now ambiguous. Both are labelled,
 * which is what makes them distinguishable to a screen reader as well as
 * to these tests.
 */
const NAV = /admin sections|bo'limlari|разделы админки/i;

describe("the protected route boundary", () => {
  it("sends an unauthenticated visitor to the login form, remembering where they were going", async () => {
    // §17.1 and §8. A bookmark to a protected section must not 404 and must
    // not render chrome — it becomes a login that knows the destination.
    stubApi({ signedIn: false });
    const router = renderAt("/users");

    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    expect(router.state.location.search).toMatchObject({ next: "/users" });
    expect(screen.queryByRole("navigation")).toBeNull();
  });

  it("returns an administrator to their intended route after signing in", async () => {
    // §17.2. The whole intended-destination round trip, through the real
    // form and the real router.
    stubApi({ signedIn: false, admin: true });
    const router = renderAt("/matches");
    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));

    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/email|pochta|почта/i), "op@example.com");
    await user.type(screen.getByLabelText(/password|parol|пароль/i), "correct");
    await user.click(screen.getByRole("button", { name: /sign in|kirish|войти/i }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/matches"));
    expect(await screen.findByRole("navigation", { name: NAV })).toBeInTheDocument();
  });

  it("never renders the shell to a valid non-administrator", async () => {
    // §17.3 — §3's rule. Authentication succeeded; authorization did not,
    // and the two are not the same event. The refusal offers a clean way
    // out and names no role.
    stubApi({ signedIn: true, admin: false });
    renderAt("/");

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).toBeNull();
  });

  it("shows nothing privileged while authority is still unresolved", async () => {
    // §17.4 — the flash. Held with a `fetch` that never settles: the
    // console must be in its checking state with no navigation, because a
    // guard that started optimistic would paint the shell first.
    vi.stubGlobal("fetch", () => new Promise<Response>(() => {}));
    renderAt("/");

    expect(await screen.findByRole("status")).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).toBeNull();
  });

  it("restores a session from the refresh cookie on a direct load", async () => {
    // §17.5. A reload loses the in-memory access token and keeps the
    // `HttpOnly` cookie, so without the refresh-first step every refresh on
    // a protected route would land on the login form despite a live
    // session.
    const { calls } = stubApi({ signedIn: true, admin: true });
    accessToken.clear();
    const router = renderAt("/tournaments");

    await waitFor(() =>
      expect(screen.getByRole("navigation", { name: NAV })).toBeInTheDocument(),
    );
    expect(router.state.location.pathname).toBe("/tournaments");
    expect(calls.some((url) => url.endsWith("/auth/browser/refresh"))).toBe(true);
  });

  it("drops the privileged UI when the role is revoked mid-session", async () => {
    // §17.7 and §10 — A64-024.1's zero-staleness property, observed from
    // the client. Nothing about the token changes; the server simply stops
    // saying yes, and the next protected navigation is refused.
    const api = stubApi({ signedIn: true, admin: true });
    const router = renderAt("/");
    await waitFor(() =>
      expect(screen.getByRole("navigation", { name: NAV })).toBeInTheDocument(),
    );

    api.state.admin = false;
    const user = userEvent.setup();
    await user.click(
      screen.getByRole("link", { name: /users|foydalanuvchilar|пользователи/i }),
    );

    await waitFor(() => expect(screen.queryByRole("navigation")).toBeNull());
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/users");
  });

  it("revokes the session server-side on sign-out and returns to the login form", async () => {
    // §17.6 and §9. A sign-out that only cleared local state would leave a
    // live session on the server — the two disagreeing is how a shared
    // machine leaks an admin session.
    const { calls } = stubApi({ signedIn: true, admin: true });
    const router = renderAt("/");
    await waitFor(() =>
      expect(screen.getByRole("navigation", { name: NAV })).toBeInTheDocument(),
    );

    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: /sign out|chiqish|выйти/i }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    expect(calls.some((url) => url.endsWith("/auth/browser/logout"))).toBe(true);
    expect(screen.queryByRole("navigation")).toBeNull();
  });

  it("marks the active section and refuses an unknown route", async () => {
    // §17.9 and §17.10. `aria-current` is what makes the active section
    // available to a screen reader rather than only to a colour, and an
    // address that is not part of the console gets an intentional page
    // rather than a blank one or a guard bypass.
    stubApi({ signedIn: true, admin: true });
    renderAt("/moderation");

    const navigation = await screen.findByRole("navigation", { name: NAV });
    const active = within(navigation)
      .getAllByRole("link")
      .filter((link) => link.getAttribute("aria-current") === "page");
    expect(active).toHaveLength(1);

    vi.unstubAllGlobals();
    stubApi({ signedIn: true, admin: true });
    renderAt("/not-a-real-section");
    expect(await screen.findByText(/not found|topilmadi|не найдена/i)).toBeInTheDocument();
  });
});

describe("the intended-destination allowlist", () => {
  it("refuses anything that could leave the admin origin", () => {
    // §8, §16 — the open redirect. `//evil.example` is the case people
    // miss: a browser reads it as an absolute URL with the current scheme,
    // so a "starts with /" check alone lets an external host through.
    expect(safeRedirect("/users")).toBe("/users");
    expect(safeRedirect("/audit?page=2")).toBe("/audit?page=2");

    expect(safeRedirect("https://evil.example")).toBe("/");
    expect(safeRedirect("//evil.example/x")).toBe("/");
    expect(safeRedirect("/\\evil.example")).toBe("/");
    expect(safeRedirect("javascript:alert(1)")).toBe("/");
    expect(safeRedirect("/login")).toBe("/");
    expect(safeRedirect(null)).toBe("/");
  });
});
