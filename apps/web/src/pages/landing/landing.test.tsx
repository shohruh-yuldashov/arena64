import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * `/`, for the audience that has no account — A64-026.1 §40.
 *
 * ## What is worth asserting here, and what is not
 *
 * The copy is not: it is reviewed by reading it, and a test that pinned a
 * sentence would fail on every edit while proving nothing about whether the
 * sentence is true. The layout is not either — that is the sweep across ten
 * renderings, recorded in §40.12.
 *
 * What a test can hold is the part a refactor breaks silently: which page
 * `/` is for whom, that the calls to action go where they say, that the
 * mobile menu is operable by keyboard, and that the auth links stay away
 * from a session that merely failed.
 */

const REFRESH = "*/api/v1/auth/browser/refresh";
const PROFILE = "*/api/v1/profile/me";
const RATINGS = "*/api/v1/ratings/me";

/** A session that resolves to `anonymous`, which is the landing's audience. */
function anonymous() {
  mswServer.use(http.post(REFRESH, () => new HttpResponse(null, { status: 401 })));
}

/** A session that resolves to `authenticated`. */
function signedIn() {
  mswServer.use(
    http.post(REFRESH, () =>
      HttpResponse.json({
        data: {
          access_token: "token",
          user: {
            id: "01a0",
            username: "player_one",
            display_name: "Player One",
            avatar: { object_key: null, version: 1, uploaded_at: null },
            preferred_language: "en",
            is_verified: true,
          },
        },
      }),
    ),
    http.get(PROFILE, () => new HttpResponse(null, { status: 500 })),
    http.get(RATINGS, () => new HttpResponse(null, { status: 500 })),
  );
}

describe("the root route", () => {
  it("is the landing page for a visitor without an account", async () => {
    anonymous();
    renderApp();

    // The heading says what the product is. Its exact words are the
    // reviewer's business; that there is one, and that it is the page's
    // only `<h1>`, is this test's.
    const heading = await screen.findByRole("heading", { level: 1 });
    expect(heading).toBeVisible();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);

    // The one thing this page exists to do.
    const register = await screen.findAllByRole("link", {
      name: /create an account|hisob yaratish|создать аккаунт/i,
    });
    expect(register[0]).toHaveAttribute("href", "/register");
    // `findAll`, not `find`: the header and the footer both offer it, which
    // is deliberate — a visitor who read to the bottom should not have to
    // scroll back up to sign in.
    const signIn = await screen.findAllByRole("link", { name: /sign in|kirish|войти/i });
    expect(signIn.length).toBeGreaterThan(0);
    for (const link of signIn) expect(link).toHaveAttribute("href", "/login");
  });

  it("keeps the product home for a player who is signed in", async () => {
    // The regression this guards: `/` grew a second page, and the first one
    // must still be there. A64-025.3's home is not replaced, it is chosen
    // between.
    signedIn();
    renderApp();

    expect(await screen.findByRole("heading", { level: 1, name: /Player One/ })).toBeVisible();
    // The shell's product navigation, which the marketing header does not
    // have — proof that the *chrome* switched too, not only the page.
    expect(
      await screen.findByRole("navigation", { name: /main|asosiy|основная/i }),
    ).toBeVisible();
  });

  it("links only to sections, never to a route a visitor cannot reach", async () => {
    // Every product route is behind `protectedPage`. A "Tournaments" link
    // in this header pointing at `/tournaments` would bounce an anonymous
    // visitor to `/login` — the defect A64-025.3 §2 refused to ship.
    anonymous();
    renderApp();

    const banner = await screen.findByRole("banner");
    for (const link of within(banner).getAllByRole("link")) {
      const href = link.getAttribute("href") ?? "";
      expect(["/", "/login", "/register"]).toContain(href.startsWith("#") ? "/" : href);
    }
  });
});

describe("the marketing header", () => {
  it("opens and closes its menu from the keyboard", async () => {
    anonymous();
    const user = userEvent.setup();
    renderApp();

    const toggle = await screen.findByRole("button", {
      name: /open menu|menyuni ochish|открыть меню/i,
    });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("keeps appearance and language reachable without an account", async () => {
    // A64-025.9B's rule: theme and language belong to the browser and stay
    // reachable in every session state. Replacing the shell's header with a
    // marketing one must not quietly drop them.
    anonymous();
    renderApp();

    expect(
      await screen.findByRole("button", {
        name: /^(Appearance and language|Ko'rinish va til|Оформление и язык)$/,
      }),
    ).toBeVisible();
  });

  it("offers no sign-in when the session merely failed", async () => {
    // `unavailable` does not mean "signed out". Offering "Sign in" here
    // tells a signed-in player they were logged out because one request
    // failed — the exact claim that state exists to avoid.
    mswServer.use(http.post(REFRESH, () => HttpResponse.error()));
    renderApp();

    const banner = await screen.findByRole("banner");
    expect(
      within(banner).queryByRole("link", { name: /sign in|kirish|войти/i }),
    ).not.toBeInTheDocument();
    expect(
      within(banner).queryByRole("link", {
        name: /create an account|hisob yaratish|создать аккаунт/i,
      }),
    ).not.toBeInTheDocument();
  });
});
