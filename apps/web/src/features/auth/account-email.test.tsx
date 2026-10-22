import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/shared/api";
import { env } from "@/shared/config/env";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * The sign-in address on `/settings/sessions` — A64-031.E.
 *
 * Through the real app, so the route guard, the settings shell, the API
 * client and the envelope all run. Every request the page makes is
 * declared here.
 */
const url = (path: string) => `${env.VITE_API_URL}${path}`;
const envelope = <T,>(data: T) => ({ data, meta: { request_id: null, correlation_id: null } });

const ACCOUNT = {
  id: "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
  username: "player_one",
  display_name: "Player One",
  email: "player.one@example.com",
  is_active: true,
  is_verified: true,
};

let meRequests: string[] = [];

function page({
  session = ACCOUNT,
  me = () => HttpResponse.json(envelope(ACCOUNT)),
}: {
  session?: typeof ACCOUNT;
  me?: () => Response;
} = {}) {
  meRequests = [];
  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: session })),
    ),
    http.get(url("/auth/browser/sessions"), () => HttpResponse.json(envelope([]))),
    http.get(url("/auth/me"), ({ request }) => {
      meRequests.push(request.url);
      return me();
    }),
  );
  renderApp({ path: "/settings/sessions" });
}

const EMAIL_GROUP = { name: /sign-in email|kirish pochtasi|почта для входа/i };

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
});

describe("the sign-in address", () => {
  it("is shown with the server's verification status", async () => {
    page();

    const group = (await screen.findByRole("heading", EMAIL_GROUP)).closest("section");
    expect(group).not.toBeNull();
    const card = within(group as HTMLElement);
    expect(await card.findByText("player.one@example.com")).toBeVisible();
    expect(card.getByText(/^(verified|tasdiqlangan|подтверждена)$/i)).toBeVisible();
  });

  it("says so when the server reports the address unverified", async () => {
    // Reachable only when the server's answer has moved since the session
    // was issued — the route guard keeps unverified sessions out — and
    // then the card must state the server's answer, not the session's.
    page({ me: () => HttpResponse.json(envelope({ ...ACCOUNT, is_verified: false })) });

    expect(
      await screen.findByText(/^(not verified|tasdiqlanmagan|не подтверждена)$/i),
    ).toBeVisible();
  });

  it("comes from /auth/me, not from the copy the session holds", async () => {
    // The session's user is whatever the last refresh returned; the
    // settings screen states the server's current answer.
    page({
      // Verified in both: an unverified session never reaches settings —
      // `RequireVerifiedEmail` sends it to `/verify-email` first.
      session: { ...ACCOUNT, email: "stale@example.com" },
      me: () => HttpResponse.json(envelope(ACCOUNT)),
    });

    expect(await screen.findByText("player.one@example.com")).toBeVisible();
    expect(screen.queryByText("stale@example.com")).not.toBeInTheDocument();
  });

  it("asks only about the signed-in account — no identifier in the request", async () => {
    page();
    await screen.findByText("player.one@example.com");

    expect(meRequests).toEqual(
      [url("/auth/me")].map((path) => new URL(path, location.href).href),
    );
  });

  it("is read-only: nothing on the card edits, re-sends or links anything", async () => {
    page();
    const group = (await screen.findByRole("heading", EMAIL_GROUP)).closest("section");
    await within(group as HTMLElement).findByText("player.one@example.com");

    const card = within(group as HTMLElement);
    expect(card.queryAllByRole("textbox")).toHaveLength(0);
    expect(card.queryAllByRole("button")).toHaveLength(0);
    expect(card.queryAllByRole("link")).toHaveLength(0);
  });

  it("wraps a long address instead of widening the page", async () => {
    // jsdom does no layout, so this asserts the decision; the reason is the
    // one A64-031.D measured for pasted text.
    const long = `${"a".repeat(64)}@${"b".repeat(60)}.example.com`;
    page({ me: () => HttpResponse.json(envelope({ ...ACCOUNT, email: long })) });

    expect(await screen.findByText(long)).toHaveClass("break-all");
  });

  it("offers a retry when the account cannot be read, and recovers", async () => {
    const user = userEvent.setup();
    let calls = 0;
    page({
      me: () => {
        calls += 1;
        return calls === 1
          ? HttpResponse.json(
              { code: "internal_error", message: "no", request_id: null, correlation_id: null },
              { status: 500 },
            )
          : HttpResponse.json(envelope(ACCOUNT));
      },
    });

    expect(
      await screen.findByText(
        /account details could not be loaded|hisob ma'lumotlarini yuklab bo'lmadi|не удалось загрузить данные аккаунта/i,
      ),
    ).toBeVisible();
    expect(screen.queryByText("player.one@example.com")).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: /try again|qayta urinish|попробовать/i }),
    );

    expect(await screen.findByText("player.one@example.com")).toBeVisible();
  });

  it("never shows the previous account's address after the session changes account", async () => {
    // The browser's refresh cookie is shared by every tab. A Google sign-in
    // completed in another tab overwrites it, and this tab's next refresh
    // then answers for a different account without any sign-out — so the
    // cache is not cleared. The card must not keep the first account's
    // address on screen under the second.
    const OTHER = {
      ...ACCOUNT,
      id: "019fb9ea-0a0c-7cec-9c5f-402727c31b77",
      email: "other@example.com",
    };
    let current = ACCOUNT;
    mswServer.use(
      http.post(url("/auth/browser/refresh"), () =>
        HttpResponse.json(envelope({ access_token: `token-${current.id}`, user: current })),
      ),
      http.get(url("/auth/browser/sessions"), () => HttpResponse.json(envelope([]))),
      http.get(url("/auth/me"), () => HttpResponse.json(envelope(current))),
      http.get(url("/thing"), ({ request }) =>
        request.headers.get("Authorization") === `Bearer token-${OTHER.id}`
          ? HttpResponse.json(envelope({ ok: true }))
          : HttpResponse.json(
              { code: "expired_token", message: "x", request_id: null, correlation_id: null },
              { status: 401 },
            ),
      ),
    );
    renderApp({ path: "/settings/sessions" });
    expect(await screen.findByText("player.one@example.com")).toBeVisible();

    // The cookie now belongs to OTHER; an expired token makes this tab refresh.
    current = OTHER;
    await api.get("/thing");

    expect(await screen.findByText("other@example.com")).toBeVisible();
    expect(screen.queryByText("player.one@example.com")).not.toBeInTheDocument();
  });

  it("is reached from settings as the account page", async () => {
    page();

    expect(
      await screen.findByRole("link", {
        name: /account & sessions|hisob va sessiyalar|аккаунт и сессии/i,
      }),
    ).toHaveAttribute("aria-current", "page");
  });
});
