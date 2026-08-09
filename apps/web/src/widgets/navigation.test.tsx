import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { env } from "@/shared/config/env";
import en from "@/shared/i18n/locales/en.json";
import ru from "@/shared/i18n/locales/ru.json";
import uz from "@/shared/i18n/locales/uz.json";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * The app shell and the product home — A64-025.3 §17.
 *
 * Everything here mounts the **real** `App`: the claims are about the shell
 * a player actually gets, and a hand-assembled header would prove only that
 * the header this file built works. That is the same argument
 * `App.test.tsx` makes for the provider graph, applied to navigation.
 *
 * The four questions these tests exist to keep answered:
 *
 *   - is `/` the product, or is it still the A64-018 exhibit?
 *   - can a player find "Play" without knowing a URL?
 *   - does the navigation say where you are, to a screen reader as well as
 *     to an eye?
 *   - can any of that be reached on a phone?
 */

const url = (path: string) => `${env.VITE_API_URL}${path}`;
const envelope = <T,>(data: T) => ({ data, meta: { request_id: null, correlation_id: null } });

const VIEWER = {
  id: "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
  username: "viewer",
  display_name: "Viewer",
  email: "viewer@example.com",
  is_active: true,
  is_verified: true,
};

function signedIn(): void {
  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: VIEWER })),
    ),
  );
}

/** The header, once the shell has settled. */
async function header(): Promise<HTMLElement> {
  return screen.findByRole("banner");
}

describe("the product home", () => {
  it("is the product, not the foundation exhibit it used to be", async () => {
    // The three strings A64-018's exhibit rendered. Asserting their absence
    // rather than the new page's presence is deliberate: this is the test
    // that fails if anybody reintroduces a developer surface at `/`, which
    // is what happened for five phases.
    signedIn();
    renderApp({ path: "/" });

    await header();
    await waitFor(() => {
      expect(screen.queryByText(/No gameplay surface is built yet/i)).not.toBeInTheDocument();
    });
    expect(screen.queryByText(/Loading primitives/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Form validation/i)).not.toBeInTheDocument();
  });

  it("puts one unmistakable way into a game in front of a signed-in player", async () => {
    signedIn();
    renderApp({ path: "/" });

    // In `main`, not merely somewhere on the page — the header links to the
    // lobby too, and a call to action that exists only in the navigation is
    // not a call to action.
    const main = await screen.findByRole("main");
    const play = await within(main).findByRole("link", { name: /^(Play|O'ynash|Играть)$/ });

    expect(play).toHaveAttribute("href", "/play");
  });

  it("has exactly one first-level heading", async () => {
    signedIn();
    renderApp({ path: "/" });

    await screen.findByRole("heading", { level: 1 });
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });
});

describe("the app shell", () => {
  it("takes the wordmark home", async () => {
    signedIn();
    renderApp({ path: "/play" });

    const brand = within(await header()).getByRole("link", { name: /Arena64/ });
    expect(brand).toHaveAttribute("href", "/");
  });

  it("marks the section a player is in, in words and not only in colour", async () => {
    signedIn();
    renderApp({ path: "/play" });

    const nav = within(await header()).getByRole("navigation", {
      name: /Main|Asosiy|Основная/,
    });
    await waitFor(() => {
      expect(within(nav).getByRole("link", { current: "page" })).toHaveAttribute(
        "href",
        "/play",
      );
    });
  });

  it("marks Tournaments current on a tournament's own page", async () => {
    // The reason `useActiveSection` asks the router rather than comparing
    // strings: a section owns routes its link does not point at, and this is
    // the cheapest case that proves it.
    signedIn();
    renderApp({ path: "/tournaments/019fb9ea-0a0c-7cec-9c5f-402727c31a96" });

    const nav = within(await header()).getByRole("navigation", {
      name: /Main|Asosiy|Основная/,
    });
    await waitFor(() => {
      expect(within(nav).getByRole("link", { current: "page" })).toHaveAttribute(
        "href",
        "/tournaments",
      );
    });
  });

  it("keeps a live game inside the Play section and history out of it", async () => {
    // `/games/$matchId` and `/games/history` share a prefix and belong to
    // different sections. A `startsWith("/games")` would light both; the
    // router does not, because a static segment beats a parameter.
    signedIn();
    renderApp({ path: "/games/history" });

    const nav = within(await header()).getByRole("navigation", {
      name: /Main|Asosiy|Основная/,
    });
    await waitFor(() => {
      expect(within(nav).getByRole("link", { current: "page" })).toHaveAttribute(
        "href",
        "/games/history",
      );
    });
  });

  it("keeps product navigation out of the account controls", async () => {
    // The regression this whole task exists to prevent: `SessionMenu` held
    // Play, Tournaments and Friends, so nobody adding a section knew where
    // to put it. The account area may link to the account and nowhere else.
    signedIn();
    renderApp({ path: "/" });

    const nav = within(await header()).getByRole("navigation", {
      name: /Main|Asosiy|Основная/,
    });
    const inNav = new Set(
      within(nav)
        .getAllByRole("link")
        .map((link) => link.getAttribute("href")),
    );

    expect(inNav).toContain("/play");
    expect(inNav).toContain("/tournaments");

    // Everything in the header that is *not* the primary navigation.
    const outside = within(await header())
      .getAllByRole("link")
      .filter((link) => !nav.contains(link))
      .map((link) => link.getAttribute("href"));

    expect(outside).not.toContain("/play");
    expect(outside).not.toContain("/tournaments");
    expect(outside).not.toContain("/friends");
  });
});

describe("the mobile navigation", () => {
  it("reaches every section from behind the menu, and closes on the way", async () => {
    signedIn();
    const person = userEvent.setup();
    renderApp({ path: "/" });

    const trigger = within(await header()).getByRole("button", {
      name: /Open menu|Menyuni ochish|Открыть меню/,
    });
    await person.click(trigger);

    const panel = await screen.findByRole("dialog");
    const nav = within(panel).getByRole("navigation", { name: /Main|Asosiy|Основная/ });
    const destinations = within(nav)
      .getAllByRole("link")
      .map((link) => link.getAttribute("href"));
    expect(destinations).toEqual(["/play", "/tournaments", "/friends", "/games/history"]);

    // A panel still open over the page the player just asked for is the
    // commonest defect in this pattern.
    await person.click(within(nav).getByRole("link", { name: /^(Play|O'ynash|Играть)$/ }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});

describe("the shell's copy", () => {
  it("says the same things in every locale the app ships", () => {
    // The home page was the one screen in the app with no translations, and
    // that is how it survived five phases of nobody noticing it was a demo.
    //
    // Types do not cover this: `TranslationKey` is derived from one locale,
    // so a key added to `uz` and forgotten in `ru` type-checks and then
    // renders a raw key path to a Russian-speaking player. Comparing the
    // three files is the only place that fact exists.
    const paths = (value: unknown, prefix = ""): string[] =>
      typeof value === "object" && value !== null
        ? Object.entries(value).flatMap(([key, child]) => paths(child, `${prefix}${key}.`))
        : [prefix.slice(0, -1)];

    const keysOf = (messages: unknown): Set<string> => new Set(paths(messages));
    const inUz = keysOf(uz);
    const inRu = keysOf(ru);
    const inEn = keysOf(en);

    expect([...inUz].filter((key) => !inRu.has(key))).toEqual([]);
    expect([...inUz].filter((key) => !inEn.has(key))).toEqual([]);
    expect([...inRu].filter((key) => !inUz.has(key))).toEqual([]);

    // And the keys this task introduced actually arrived.
    for (const key of [
      "layout.primaryNav",
      "layout.accountNav",
      "layout.home",
      "layout.skipToContent",
      "home.greeting",
      "home.playCta",
      "home.subtitle",
      "home.moreTitle",
    ]) {
      expect(inUz, key).toContain(key);
    }
  });
});
