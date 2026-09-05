import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { refresh } from "@/features/auth/api";
import type { AuthChannel } from "@/features/auth/model/auth-channel";
import { safeRedirect } from "@/features/auth/model/safe-redirect";
import { api } from "@/shared/api";
import { httpClient } from "@/shared/api/client";
import { env } from "@/shared/config/env";
import { mswServer } from "@/shared/test/msw/server";
import { openAccountMenu, renderApp } from "@/shared/test/render";

/**
 * Authentication, through the real app — A64-020.2 §20.
 *
 * Every test here mounts `App`: the real provider graph, the real router,
 * the real Axios instance and its real interceptors. That is not
 * thoroughness for its own sake — it is the only way to assert the things
 * this phase is actually about. A `SessionProvider` rendered by hand would
 * prove the provider works and say nothing about whether the app has one,
 * and an interceptor tested in isolation would say nothing about whether
 * the one that ships is registered.
 *
 * MSW intercepts at the network, so the cookie, the envelope, the error
 * normalisation and the retry all run for real.
 */
const url = (path: string) => `${env.VITE_API_URL}${path}`;
const REFRESH = url("/auth/browser/refresh");
const LOGIN = url("/auth/browser/login");
const LOGOUT = url("/auth/browser/logout");

const USER = {
  id: "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
  username: "player_one",
  display_name: "Player One",
  email: "player@example.com",
  is_active: true,
  is_verified: true,
};

function sessionBody(accessToken: string) {
  return {
    data: { access_token: accessToken, token_type: "Bearer", expires_in: 900, user: USER },
    meta: { request_id: null, correlation_id: null },
  };
}

/** `401 invalid_session` — what the API answers when there is no cookie. */
function noSession() {
  return HttpResponse.json(
    { code: "invalid_session", message: "No session.", request_id: null, correlation_id: null },
    { status: 401 },
  );
}

beforeEach(() => {
  // React logs every caught error; these tests provoke several on purpose.
  vi.spyOn(console, "error").mockImplementation(() => {});
  // The interceptors are registered per mount and ejected on unmount, but a
  // test that throws mid-render can leak one. Clearing here means a leaked
  // interceptor cannot make the *next* test pass or fail.
  httpClient.interceptors.request.clear();
  httpClient.interceptors.response.clear();
});

describe("session bootstrap", () => {
  it("resolves to exactly one of authenticated, anonymous or unavailable", async () => {
    // Three outcomes, one test, because the mistake is in telling them
    // apart rather than in any one of them. The cookie is `HttpOnly`, so
    // the app never sees it — it calls refresh and finds out.

    // --- 200: authenticated ---
    mswServer.use(http.post(REFRESH, () => HttpResponse.json(sessionBody("token-1"))));
    const signedIn = renderApp();
    // The header renders the account, which means the token and the user
    // both reached memory through the real provider graph.
    expect(await screen.findByText("Player One")).toBeVisible();
    await openAccountMenu(userEvent.setup());
    expect(screen.getByRole("button", { name: /sign out|chiqish|выйти/i })).toBeVisible();
    signedIn.unmount();

    // --- 401: anonymous. A fact, and the commonest one. ---
    mswServer.use(http.post(REFRESH, () => noSession()));
    const anonymous = renderApp();
    // Scoped to the header: A64-025.3 gave the signed-out home its own
    // sign-in call to action, so the name is no longer unique on the page.
    // The claim here is about the *shell* reflecting an anonymous session,
    // which is the one this file is about.
    const anonymousHeader = await screen.findByRole("banner");
    expect(
      await within(anonymousHeader).findByRole("link", { name: /sign in|kirish|войти/i }),
    ).toBeVisible();
    anonymous.unmount();

    // --- network failure: **neither** ---
    // Asserting the absence of the sign-in link is the point. Rendering it
    // would mean the app decided the user was signed out because one
    // request failed — discarding a live session and asking them to
    // authenticate again for nothing.
    mswServer.use(http.post(REFRESH, () => HttpResponse.error()));
    renderApp();
    const unavailableHeader = await screen.findByRole("banner");
    await waitFor(() => {
      expect(screen.queryByText("Player One")).not.toBeInTheDocument();
    });
    expect(
      within(unavailableHeader).queryByRole("link", { name: /sign in|kirish|войти/i }),
    ).not.toBeInTheDocument();
  });
});

describe("token refresh", () => {
  it("refreshes once for concurrent 401s and retries each request", async () => {
    // The defect this exists for: the backend rotates the refresh token on
    // every use and revokes the chain when a rotated one is presented. Five
    // parallel 401s that each refresh would sign the user out — of their own
    // app, on a normal page load, unreproducibly.
    let refreshes = 0;
    let issued = 0;

    mswServer.use(
      http.post(REFRESH, () => {
        refreshes += 1;
        return HttpResponse.json(sessionBody(`token-${++issued}`));
      }),
      http.get(url("/api/v1/thing"), ({ request }) =>
        request.headers.get("Authorization") === "Bearer token-2"
          ? HttpResponse.json({ data: { ok: true }, meta: {} })
          : HttpResponse.json(
              {
                code: "expired_token",
                message: "Expired.",
                request_id: null,
                correlation_id: null,
              },
              { status: 401 },
            ),
      ),
    );

    renderApp();
    await screen.findByText("Player One"); // bootstrap consumed refresh #1

    const results = await Promise.all([
      api.get("/api/v1/thing"),
      api.get("/api/v1/thing"),
      api.get("/api/v1/thing"),
      api.get("/api/v1/thing"),
      api.get("/api/v1/thing"),
    ]);

    // One refresh for all five — the single-flight promise — and every
    // request succeeded on its retry with the rotated token.
    expect(refreshes).toBe(2); // the bootstrap, then exactly one more
    expect(results).toEqual(Array.from({ length: 5 }, () => ({ ok: true })));
  });
});

describe("the login form", () => {
  it("signs in and shows one bounded message for a rejected credential", async () => {
    const user = userEvent.setup();
    mswServer.use(
      http.post(REFRESH, () => noSession()),
      http.post(LOGIN, async ({ request }) => {
        const body = (await request.json()) as { password: string };
        return body.password === "CorrectHorse1!"
          ? HttpResponse.json(sessionBody("token-1"))
          : HttpResponse.json(
              {
                code: "invalid_credentials",
                message: "Invalid email or password.",
                request_id: null,
                correlation_id: null,
              },
              { status: 401 },
            );
      }),
    );

    renderApp({ path: "/login" });

    const email = await screen.findByLabelText(/email|pochta|почта/i);
    // `selector` because A64-025.4 put a "Show password" button beside the
    // input, and its accessible name matches the same words the label does.
    const password = screen.getByLabelText(/password|parol|пароль/i, { selector: "input" });

    // --- the rejection ---
    await user.type(email, "player@example.com");
    await user.type(password, "wrong-password");
    await user.click(screen.getByRole("button", { name: /sign in|kirish|войти/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toBeVisible();
    // Focus moves to the message, or a screen-reader user is left at the
    // bottom of a form that now says something they cannot see.
    expect(alert).toHaveFocus();
    // The backend's English prose never reaches the DOM — a translated key
    // does. And nothing distinguishes "no such account" from "wrong
    // password", which is what stops this being an enumeration oracle.
    expect(alert.textContent).not.toContain("Invalid email or password.");

    // --- the success ---
    await user.clear(password);
    await user.type(password, "CorrectHorse1!");
    await user.click(screen.getByRole("button", { name: /sign in|kirish|войти/i }));

    // Signed in, and moved off `/login` — the header proves the session
    // reached memory and the guard let the redirect through.
    expect(await screen.findByText("Player One")).toBeVisible();
  });
});

describe("sign-out", () => {
  it("clears the session and the private cache, and tells the other tabs", async () => {
    const user = userEvent.setup();
    const posted: unknown[] = [];
    const channel: AuthChannel = {
      post: (message) => posted.push(message),
      subscribe: () => () => {},
      close: () => {},
    };

    mswServer.use(
      http.post(REFRESH, () => HttpResponse.json(sessionBody("token-1"))),
      http.post(LOGOUT, () => new HttpResponse(null, { status: 204 })),
    );

    const { queryClient } = renderApp({ channel });
    await screen.findByText("Player One");

    // Something a previous user's session fetched. It must not survive:
    // every cached query was fetched *as somebody*.
    queryClient.setQueryData(["private", "thing"], { secret: true });

    await openAccountMenu(user);
    await user.click(screen.getByRole("button", { name: /sign out|chiqish|выйти/i }));

    await waitFor(() => {
      expect(screen.queryByText("Player One")).not.toBeInTheDocument();
    });
    expect(queryClient.getQueryData(["private", "thing"])).toBeUndefined();
    // The fact, and nothing else. A token on a channel any script on the
    // origin can read would be a token in `localStorage` with extra steps.
    expect(posted).toEqual([{ type: "logged_out" }]);
    expect(JSON.stringify(posted)).not.toContain("token-1");
  });
});

describe("the next redirect", () => {
  it("returns to an in-app path and refuses everything else", () => {
    // Not a rendering test: this is a pure function guarding an open
    // redirect, and the interesting inputs are the ones a browser resolves
    // differently from how a naive check reads them.
    expect(safeRedirect("/tournaments/42")).toBe("/tournaments/42");
    expect(safeRedirect("/tournaments?page=2")).toBe("/tournaments?page=2");

    for (const hostile of [
      "https://evil.example/login", // another origin outright
      "//evil.example", // protocol-relative — `startsWith("/")` passes it
      "/\\evil.example", // backslash form of the same
      "\\\\evil.example",
      "javascript:alert(1)", // a scheme, not a path
      "/\tjavascript:alert(1)", // whitespace some browsers strip first
      "%2f%2fevil.example", // encoded, decoded once and re-judged
      "%zz", // malformed encoding — unreadable, so not navigated to
      "tournaments", // relative: resolves against wherever the user was
      "/login", // circular
    ]) {
      expect(safeRedirect(hostile), hostile).toBe("/");
    }

    expect(safeRedirect(null)).toBe("/");
    expect(safeRedirect("")).toBe("/");
  });
});

describe("a rotation lost to another tab", () => {
  // A64-028.2 §14. A browser shares one cookie jar across its tabs, so two
  // tabs refreshing together present the same token and the server answers
  // the loser with `409`. The successor is in that shared jar, so the loser
  // asks again — which is the difference between a tab that stays signed in
  // and the sign-out A64-028.1 measured.
  it("presents the cookie again and stays signed in", async () => {
    let attempts = 0;
    mswServer.use(
      http.post(REFRESH, () => {
        attempts += 1;
        return attempts === 1
          ? HttpResponse.json(
              {
                code: "session_rotation_conflict",
                message: "This session was refreshed by another request.",
                request_id: null,
                correlation_id: null,
              },
              { status: 409, headers: { "Retry-After": "1" } },
            )
          : HttpResponse.json(sessionBody("token-after-the-race"));
      }),
    );

    await expect(refresh()).resolves.toMatchObject({
      access_token: "token-after-the-race",
    });
    expect(attempts).toBe(2);
  });

  it("gives up rather than looping when the conflict does not clear", async () => {
    let attempts = 0;
    mswServer.use(
      http.post(REFRESH, () => {
        attempts += 1;
        return HttpResponse.json(
          {
            code: "session_rotation_conflict",
            message: "Conflict.",
            request_id: null,
            correlation_id: null,
          },
          { status: 409 },
        );
      }),
    );

    await expect(refresh()).rejects.toMatchObject({ status: 409 });
    expect(attempts).toBe(3);
  });

  it("does not retry a 401 — a missing cookie is not a race", async () => {
    let attempts = 0;
    mswServer.use(
      http.post(REFRESH, () => {
        attempts += 1;
        return HttpResponse.json(
          { code: "invalid_session", message: "No.", request_id: null, correlation_id: null },
          { status: 401 },
        );
      }),
    );

    await expect(refresh()).rejects.toMatchObject({ status: 401 });
    expect(attempts).toBe(1);
  });
});

// A cheap guard against the one storage mistake this design exists to
// prevent. Not a test of its own — it runs after each of the above, where a
// real session has actually been through the store.
afterEach(() => {
  const stored = JSON.stringify({ ...localStorage, ...sessionStorage });
  expect(stored).not.toContain("token-");
  expect(stored).not.toContain("access_token");
});
