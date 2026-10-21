import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { httpClient } from "@/shared/api/client";
import { env } from "@/shared/config/env";
import { failure, ok } from "@/shared/test/msw/handlers";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * "Continue with Google", through the real app — A64-031.E1.
 *
 * The browser half only: the button, what `?google=` says, and the two
 * pages a sign-in can stop at. The round trip itself is redirects between
 * the API and Google, which no jsdom test can follow; the API's contract
 * suite drives it over HTTP, and the manual check against real Google is
 * in `docs/05-operations/google-sign-in.md`.
 */
const url = (path: string) => `${env.VITE_API_URL}${path}`;
const REFRESH = url("/auth/browser/refresh");
const PROVIDERS = url("/auth/browser/providers");
const PENDING = url("/auth/browser/google/pending");
const ACCOUNT = url("/auth/browser/google/account");
const LINK = url("/auth/browser/google/link");

const USER = {
  id: "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
  username: "player_one",
  display_name: "Player One",
  email: "player@gmail.com",
  is_active: true,
  is_verified: true,
};

function session() {
  return HttpResponse.json(
    {
      data: { access_token: "token-1", token_type: "Bearer", expires_in: 900, user: USER },
      meta: { request_id: null, correlation_id: null },
    },
    { status: 201 },
  );
}

function rejected(status: number, code: string) {
  return HttpResponse.json(
    { code, message: "Nope.", request_id: null, correlation_id: null },
    { status },
  );
}

const BACK_TO_SIGN_IN = /go to sign in|kirish sahifasiga|к входу/i;

let refreshes = 0;

/** No session, and Google offered unless a test says otherwise. */
function anonymous({ google = true }: { google?: boolean } = {}) {
  refreshes = 0;
  mswServer.use(
    http.post(REFRESH, () => {
      refreshes += 1;
      return rejected(401, "invalid_session");
    }),
    ok(PROVIDERS, { google }),
  );
}

/**
 * jsdom cannot navigate, so a followed link is stopped once React has seen
 * the click. Links only — a submit button's default is the submission.
 */
function holdNavigation(event: MouseEvent) {
  if (event.target instanceof Element && event.target.closest("a[href]"))
    event.preventDefault();
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  httpClient.interceptors.request.clear();
  httpClient.interceptors.response.clear();
  window.addEventListener("click", holdNavigation);
});

afterEach(() => {
  window.removeEventListener("click", holdNavigation);
});

describe("the Google button", () => {
  it("starts the flow at the API, carrying where the person was going", async () => {
    anonymous();
    renderApp({ path: "/login?next=%2Ftournaments%2F7" });

    const link = await screen.findByRole("link", {
      name: /continue with google|google orqali|с google/i,
    });

    expect(link).toHaveAttribute(
      "href",
      `${env.VITE_API_URL}/auth/browser/google/start?next=%2Ftournaments%2F7`,
    );
  });

  it("never carries a destination off the platform", async () => {
    anonymous();
    renderApp({ path: "/login?next=https%3A%2F%2Fevil.example" });

    const link = await screen.findByRole("link", {
      name: /continue with google|google orqali|с google/i,
    });

    expect(link.getAttribute("href")).toMatch(/\?next=%2F$/);
  });

  it("is absent when the deployment does not offer Google", async () => {
    anonymous({ google: false });
    renderApp({ path: "/login" });

    await screen.findByRole("button", { name: /sign in|kirish|войти/i });

    expect(screen.queryByRole("link", { name: /google/i })).not.toBeInTheDocument();
  });

  it("is offered on the registration page too", async () => {
    anonymous();
    renderApp({ path: "/register" });

    expect(
      await screen.findByRole("link", { name: /continue with google|google orqali|с google/i }),
    ).toBeVisible();
  });

  it("cannot be activated twice", async () => {
    const user = userEvent.setup();
    anonymous();
    renderApp({ path: "/login" });
    const link = await screen.findByRole("link", {
      name: /continue with google|google orqali|с google/i,
    });

    await user.click(link);

    const busy = screen.getByRole("link", {
      name: /opening google|google ochilmoqda|открываем google/i,
    });
    expect(busy).toHaveAttribute("aria-disabled", "true");
  });
});

describe("what a Google round trip came back to say", () => {
  it.each([
    ["cancelled", /was cancelled|bekor qilindi|отменён/i],
    ["email_unverified", /not confirmed|tasdiqlamagan|не подтвердил/i],
    ["expired", /expired|muddati tugagan|истёк/i],
  ])("names ?google=%s", async (outcome, message) => {
    anonymous();
    renderApp({ path: `/login?google=${outcome}` });

    expect(await screen.findByText(message)).toBeVisible();
  });

  it("says nothing for a reason it does not know", async () => {
    anonymous();
    renderApp({ path: "/login?google=constructor" });

    await screen.findByRole("button", { name: /sign in|kirish|войти/i });

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("choosing a username after Google", () => {
  it("creates the account and goes where the person was going", async () => {
    const user = userEvent.setup();
    let body: unknown = null;
    anonymous();
    mswServer.use(
      ok(PENDING, { purpose: "create_account", email: "player@gmail.com", next: "/play" }),
      http.post(ACCOUNT, async ({ request }) => {
        body = await request.json();
        return session();
      }),
    );
    const { router } = renderApp({ path: "/register/google" });

    expect(await screen.findByText(/player@gmail\.com/)).toBeVisible();
    await user.type(
      screen.getByLabelText(/username|foydalanuvchi nomi|имя пользователя/i),
      "player_one",
    );
    await user.click(
      screen.getByRole("button", { name: /create account|hisob yaratish|создать аккаунт/i }),
    );

    expect(await screen.findByText("Player One")).toBeVisible();
    expect(body).toMatchObject({ username: "player_one" });
    await waitFor(() => expect(router.state.location.pathname).toBe("/play"));
  });

  it("names a taken username and keeps the person on the page", async () => {
    const user = userEvent.setup();
    anonymous();
    mswServer.use(
      ok(PENDING, { purpose: "create_account", email: "player@gmail.com", next: "/" }),
      http.post(ACCOUNT, () => rejected(409, "username_already_exists")),
    );
    renderApp({ path: "/register/google" });

    await user.type(
      await screen.findByLabelText(/username|foydalanuvchi nomi|имя пользователя/i),
      "taken_name",
    );
    await user.click(
      screen.getByRole("button", { name: /create account|hisob yaratish|создать аккаунт/i }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(/taken|band|занято/i);
  });

  it("says when nothing is pending, without treating it as a lost session", async () => {
    anonymous();
    mswServer.use(failure(PENDING, 401, "external_sign_in_expired"));
    renderApp({ path: "/register/google" });

    expect(await screen.findByRole("alert")).toHaveTextContent(/expired|muddati tugadi|истёк/i);
    expect(screen.getByRole("link", { name: BACK_TO_SIGN_IN })).toBeVisible();
    // The bootstrap's refresh, and none for the pending check's 401.
    expect(refreshes).toBe(1);
  });

  it("sends an identity waiting for a password to the password page", async () => {
    anonymous();
    mswServer.use(
      ok(PENDING, { purpose: "link_account", email: "taken@gmail.com", next: "/" }),
    );
    renderApp({ path: "/register/google" });

    expect(
      await screen.findByRole("heading", {
        name: /connect google|google'ni|подключите google/i,
      }),
    ).toBeVisible();
  });
});

describe("connecting Google to an existing account", () => {
  it("asks for that account's password, and only that", async () => {
    anonymous();
    mswServer.use(
      ok(PENDING, { purpose: "link_account", email: "taken@gmail.com", next: "/" }),
    );
    renderApp({ path: "/connect-google" });

    expect(await screen.findByText(/taken@gmail\.com/)).toBeVisible();
    const form = screen
      .getByRole("button", { name: /connect and sign in|ulash va kirish|подключить и войти/i })
      .closest("form");
    expect(form).not.toBeNull();
    expect(within(form as HTMLElement).queryAllByRole("textbox")).toHaveLength(0);
  });

  it("a wrong password is the ordinary wrong-password message", async () => {
    const user = userEvent.setup();
    anonymous();
    mswServer.use(
      ok(PENDING, { purpose: "link_account", email: "taken@gmail.com", next: "/" }),
      http.post(LINK, () => rejected(401, "invalid_credentials")),
    );
    renderApp({ path: "/connect-google" });

    await user.type(
      await screen.findByLabelText(/password|parol|пароль/i, { selector: "input" }),
      "WrongHorse9?",
    );
    await user.click(
      screen.getByRole("button", {
        name: /connect and sign in|ulash va kirish|подключить и войти/i,
      }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /password is wrong|parol noto'g'ri|или пароль/i,
    );
    expect(refreshes).toBe(1);
  });

  it("the right password signs in", async () => {
    const user = userEvent.setup();
    anonymous();
    mswServer.use(
      ok(PENDING, { purpose: "link_account", email: "taken@gmail.com", next: "/" }),
      http.post(LINK, () => session()),
    );
    renderApp({ path: "/connect-google" });

    await user.type(
      await screen.findByLabelText(/password|parol|пароль/i, { selector: "input" }),
      "CorrectHorse1!",
    );
    await user.click(
      screen.getByRole("button", {
        name: /connect and sign in|ulash va kirish|подключить и войти/i,
      }),
    );

    expect(await screen.findByText("Player One")).toBeVisible();
  });
});
