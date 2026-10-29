import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import { env } from "@/shared/config/env";
import { LOCALE_STORAGE_KEY } from "@/shared/i18n";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * The leaderboard page — A64-033.
 *
 * Through the real app with the network replaced. The server decides who
 * is ranked; what is asserted here is that the page renders exactly that,
 * asks for the ladder the URL names, never touches a preference while
 * filtering, and says the right thing when a ladder is empty, still being
 * searched, or failed.
 */

const url = (path: string) => `${env.VITE_API_URL}${path}`;
const ME = "019fb9ea-0a0c-7cec-9c5f-402727c31a96";

function envelope<T>(data: T) {
  return { data, meta: { request_id: null, correlation_id: null } };
}

function player(rank: number, overrides: Record<string, unknown> = {}) {
  return {
    rank,
    player_id: `019fb9ea-0a0c-7cec-9c5f-40272700000${String(rank)}`,
    username: `player${String(rank)}`,
    display_name: `Player ${String(rank)}`,
    country: null,
    rating: 1800.4 - rank * 10,
    games_played: 40,
    is_provisional: false,
    ...overrides,
  };
}

function page(players: unknown[], next: string | null = null, country: string | null = null) {
  return envelope({
    variant: "russian_8x8",
    speed_class: "blitz",
    country,
    players,
    next_cursor: next,
  });
}

function standing(status: string, overrides: Record<string, unknown> = {}) {
  return envelope({
    variant: "russian_8x8",
    speed_class: "blitz",
    country: null,
    status,
    rank: status === "ranked" ? 2 : null,
    rating: status === "unrated" ? null : 1612.6,
    games_played: status === "unrated" ? null : 30,
    is_provisional: status === "unrated" ? null : false,
    ...overrides,
  });
}

/** Signed in, with a blitz rating so the page opens on blitz. */
function signedIn() {
  const requested: { path: string; params: URLSearchParams }[] = [];
  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json(
        envelope({
          access_token: "token-1",
          token_type: "Bearer",
          expires_in: 900,
          user: {
            id: ME,
            username: "me",
            display_name: "Me",
            email: "me@example.com",
            is_active: true,
            is_verified: true,
          },
        }),
      ),
    ),
    http.get(url("/ratings/me"), () =>
      HttpResponse.json(
        envelope({
          player_id: ME,
          ratings: [
            {
              variant: "russian_8x8",
              speed_class: "blitz",
              rating: 1612.6,
              deviation: 70,
              games_played: 30,
              is_provisional: false,
              peak: 1650,
            },
          ],
        }),
      ),
    ),
  );
  return requested;
}

function ladder(
  respond: (params: URLSearchParams) => ReturnType<typeof page>,
  me: ReturnType<typeof standing> = standing("ranked"),
) {
  const asked: URLSearchParams[] = [];
  mswServer.use(
    http.get(url("/leaderboard/players"), ({ request }) => {
      const params = new URL(request.url).searchParams;
      asked.push(params);
      return HttpResponse.json(respond(params));
    }),
    http.get(url("/leaderboard/players/me"), () => HttpResponse.json(me)),
  );
  return asked;
}

beforeEach(() => {
  localStorage.setItem(LOCALE_STORAGE_KEY, "en");
});

describe("the ladder", () => {
  it("renders the server's ranks, names and marks, and your own row", async () => {
    signedIn();
    ladder(() =>
      page([
        player(1, { country: "UZ" }),
        player(2, { player_id: ME, username: "me", display_name: "Me" }),
        player(3, { is_provisional: true }),
      ]),
    );
    renderApp({ path: "/leaderboard" });

    const table = await screen.findByRole("table", { name: /blitz leaderboard/i });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("cell")[0]?.textContent)).toEqual([
      "1",
      "2",
      "3",
    ]);
    expect(
      within(rows[0] as HTMLElement).getByRole("link", { name: "Player 1" }),
    ).toHaveAttribute("href", "/players/player1");
    // The flag is decoration; the country is still read out.
    expect(rows[0]).toHaveTextContent("Uzbekistan");
    expect(within(rows[1] as HTMLElement).getByText("You")).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText("Provisional")).toBeInTheDocument();
  });

  it("opens on the class you play most and says where you stand", async () => {
    signedIn();
    const asked = ladder(() => page([player(1)]));
    renderApp({ path: "/leaderboard" });

    await screen.findByRole("table");
    expect(asked[0]?.get("speed_class")).toBe("blitz");
    expect(screen.getByRole("link", { name: "Blitz" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByText("#2")).toBeInTheDocument();
  });

  it("asks for the other class's ladder when its tab is chosen", async () => {
    signedIn();
    const asked = ladder(() => page([player(1)]));
    renderApp({ path: "/leaderboard" });

    await screen.findByRole("table");
    await userEvent.click(screen.getByRole("link", { name: "Rapid" }));

    await waitFor(() => expect(asked.some((p) => p.get("speed_class") === "rapid")).toBe(true));
    expect(screen.getByRole("link", { name: "Rapid" })).toHaveAttribute("aria-current", "page");
  });
});

describe("the country filter", () => {
  it("asks for that country's ladder and changes nothing about you", async () => {
    signedIn();
    const patches: string[] = [];
    mswServer.use(
      http.patch(url("/profile"), ({ request }) => {
        patches.push(request.url);
        return HttpResponse.json({});
      }),
    );
    const asked = ladder((params) =>
      page([player(1, { country: params.get("country") })], null, params.get("country")),
    );
    renderApp({ path: "/leaderboard" });

    await screen.findByRole("table");
    await userEvent.selectOptions(screen.getByLabelText("Country"), "UZ");

    await waitFor(() => expect(asked.some((p) => p.get("country") === "UZ")).toBe(true));
    expect(patches).toEqual([]);
    expect(localStorage.getItem(LOCALE_STORAGE_KEY)).toBe("en");
    expect(document.documentElement.lang).toBe("en");
  });

  it("drops a country or class in the URL that is not one", async () => {
    signedIn();
    const asked = ladder(() => page([player(1)]));
    renderApp({ path: "/leaderboard?speed=hyper&country=XX" });

    await screen.findByRole("table");
    expect(asked[0]?.get("speed_class")).toBe("blitz");
    expect(asked[0]?.has("country")).toBe(false);
  });

  it("keeps looking down a sparse country's ladder instead of calling it empty", async () => {
    signedIn();
    ladder((params) =>
      params.get("after") === null
        ? page([], "cursor-2", "UZ")
        : page([player(1, { country: "UZ" })], null, "UZ"),
    );
    renderApp({ path: "/leaderboard?country=UZ" });

    const table = await screen.findByRole("table");
    expect(within(table).getByRole("link", { name: "Player 1" })).toBeInTheDocument();
    expect(screen.queryByText(/Nobody from Uzbekistan/)).toBeNull();
  });

  it("says nobody from a country is ranked only once the ladder is exhausted", async () => {
    signedIn();
    ladder(() => page([], null, "UZ"), standing("not_listed", { country: "UZ" }));
    renderApp({ path: "/leaderboard?country=UZ" });

    expect(
      await screen.findByText("Nobody from Uzbekistan is ranked in this speed class yet"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Not on this ladder — your profile doesn't show Uzbekistan"),
    ).toBeInTheDocument();
  });
});

describe("states", () => {
  it("tells an unrated player so and where to fix it", async () => {
    signedIn();
    ladder(() => page([player(1)]), standing("unrated"));
    renderApp({ path: "/leaderboard?speed=blitz" });

    expect(await screen.findByText("No Blitz rating yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Play a rated game" })).toHaveAttribute(
      "href",
      "/play",
    );
  });

  it("says further down rather than inventing a rank", async () => {
    signedIn();
    ladder(() => page([player(1)]), standing("beyond"));
    renderApp({ path: "/leaderboard" });

    expect(await screen.findByText("Further down than ranks are counted")).toBeInTheDocument();
  });

  it("offers a retry when the ladder could not be read", async () => {
    signedIn();
    let calls = 0;
    mswServer.use(
      http.get(url("/leaderboard/players"), () => {
        calls += 1;
        return calls === 1
          ? HttpResponse.json(
              { code: "unavailable", message: "Down.", request_id: null, correlation_id: null },
              { status: 503 },
            )
          : HttpResponse.json(page([player(1)]));
      }),
      http.get(url("/leaderboard/players/me"), () => HttpResponse.json(standing("ranked"))),
    );
    renderApp({ path: "/leaderboard" });

    await userEvent.click(await screen.findByRole("button", { name: /try again/i }));

    expect(await screen.findByRole("table")).toBeInTheDocument();
  });
});

describe("navigation", () => {
  it("is a section of the app header", async () => {
    signedIn();
    ladder(() => page([player(1)]));
    renderApp({ path: "/leaderboard" });

    await screen.findByRole("table");
    const links = screen.getAllByRole("link", { name: "Leaderboard" });
    expect(links.some((link) => link.getAttribute("href") === "/leaderboard")).toBe(true);
  });
});
