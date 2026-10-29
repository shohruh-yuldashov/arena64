import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import { env } from "@/shared/config/env";
import { LOCALE_STORAGE_KEY } from "@/shared/i18n";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * A profile's games and rating line — A64-032.
 *
 * Through the real app and router, with only the network replaced, so what
 * is asserted is what a player reads: whose seat a result is said from,
 * when a rating change appears and when it must not, what the line draws
 * from, and what an empty or failed read looks like.
 */

const url = (path: string) => `${env.VITE_API_URL}${path}`;

const ME_ID = "019fb9ea-0a0c-7cec-9c5f-402727c31a96";
const OWNER_ID = "019fb9ea-0a0c-7cec-9c5f-402727c31a97";
const RIVAL_ID = "019fb9ea-0a0c-7cec-9c5f-402727c31a98";

function envelope<T>(data: T) {
  return { data, meta: { request_id: null, correlation_id: null } };
}

const STATISTICS = {
  games_played: 10,
  wins: 4,
  losses: 3,
  draws: 3,
  win_rate: 0.4,
  current_rating: 1500,
  highest_rating: 1500,
  current_streak: 1,
  best_win_streak: 2,
};

function rating(speed_class: string, games_played: number, value = 1500) {
  return {
    variant: "russian_8x8",
    speed_class,
    rating: value,
    deviation: 80,
    games_played,
    is_provisional: games_played < 10,
  };
}

/** One finished rated match, light against dark, light winning. */
function match(
  id: number,
  light: string,
  dark: string,
  change?: { before: number; after: number },
) {
  const opponent = light === OWNER_ID || light === ME_ID ? dark : light;
  return {
    match_id: `00000000-0000-4000-9000-${String(id).padStart(12, "0")}`,
    variant: "russian_8x8",
    speed_class: "blitz",
    rated: true,
    engine_version: 2,
    light_player_id: light,
    dark_player_id: dark,
    opponent_id: opponent,
    outcome: "win",
    termination_reason: "resignation",
    winner: "light",
    ply_number: 40,
    opponent: {
      player_id: opponent,
      username: "rival",
      display_name: "The Rival",
      avatar_thumbnail_url: null,
      rating_value: null,
      rating_deviation: null,
      is_provisional: null,
    },
    time_control: { initial_ms: 180_000, increment_ms: 2_000 },
    rating:
      change === undefined
        ? null
        : { before: change.before, after: change.after, delta: change.after - change.before },
    started_at: "2026-10-20T10:00:00Z",
    ended_at: "2026-10-20T10:20:00Z",
  };
}

function signedIn() {
  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json(
        envelope({
          access_token: "token-1",
          token_type: "Bearer",
          expires_in: 900,
          user: {
            id: ME_ID,
            username: "me",
            display_name: "Me",
            email: "me@example.com",
            is_active: true,
            is_verified: true,
          },
        }),
      ),
    ),
    http.get(url(`/players/${ME_ID}/tournaments`), () =>
      HttpResponse.json(envelope({ entries: [], next_cursor: null })),
    ),
    http.get(url(`/players/${OWNER_ID}/tournaments`), () =>
      HttpResponse.json(envelope({ entries: [], next_cursor: null })),
    ),
  );
}

function myProfile(ratings: ReturnType<typeof rating>[]) {
  mswServer.use(
    http.get(url("/profile/me"), () =>
      HttpResponse.json(
        envelope({
          id: ME_ID,
          username: "me",
          display_name: "Me",
          bio: null,
          country: null,
          avatar_url: null,
          thumbnail_url: null,
          joined_at: "2026-01-15T10:00:00Z",
          is_online: true,
          last_seen: null,
          statistics: STATISTICS,
        }),
      ),
    ),
    http.get(url("/ratings/me"), () =>
      HttpResponse.json(envelope({ player_id: ME_ID, ratings })),
    ),
  );
}

function someoneElsesProfile() {
  mswServer.use(
    http.get(url("/profiles/owner"), () =>
      HttpResponse.json(
        envelope({
          id: OWNER_ID,
          username: "owner",
          display_name: "The Owner",
          avatar_url: null,
          thumbnail_url: null,
          country: null,
          language: "en",
          bio: null,
          joined_at: "2026-02-01T10:00:00Z",
          statistics: STATISTICS,
          relationship: null,
          ratings: { classic: null, rapid: null, blitz: null },
        }),
      ),
    ),
    http.get(url(`/players/${OWNER_ID}/ratings`), () =>
      HttpResponse.json(envelope({ player_id: OWNER_ID, ratings: [rating("blitz", 12)] })),
    ),
  );
}

beforeEach(() => {
  localStorage.setItem(LOCALE_STORAGE_KEY, "en");
});

describe("recent games on somebody else's profile", () => {
  it("says the result from the profile owner's seat and names their opponent", async () => {
    signedIn();
    someoneElsesProfile();
    mswServer.use(
      http.get(url(`/players/${OWNER_ID}/matches`), () =>
        HttpResponse.json(
          // The owner played dark and lost — light won.
          envelope({ entries: [match(1, RIVAL_ID, OWNER_ID)], next_cursor: null }),
        ),
      ),
    );
    renderApp({ path: "/players/owner" });

    const section = await screen.findByRole("region", { name: "Recent games" });
    const row = await within(section).findByRole("listitem");
    expect(within(row).getByText("Lost")).toBeInTheDocument();
    expect(within(row).getByText("vs The Rival")).toBeInTheDocument();
  });

  it("shows no rating change, because only the owner is told that", async () => {
    signedIn();
    someoneElsesProfile();
    mswServer.use(
      http.get(url(`/players/${OWNER_ID}/matches`), () =>
        HttpResponse.json(
          envelope({ entries: [match(1, OWNER_ID, RIVAL_ID)], next_cursor: null }),
        ),
      ),
    );
    renderApp({ path: "/players/owner" });

    const section = await screen.findByRole("region", { name: "Recent games" });
    await within(section).findByRole("listitem");
    expect(within(section).queryByText(/Rating [+−±]/)).toBeNull();
    // And it offers no "all games": there is no page for another player's
    // whole record.
    expect(within(section).queryByRole("link", { name: "All games" })).toBeNull();
  });

  it("says when the player has no finished games rather than drawing an empty frame", async () => {
    signedIn();
    someoneElsesProfile();
    mswServer.use(
      http.get(url(`/players/${OWNER_ID}/matches`), () =>
        HttpResponse.json(envelope({ entries: [], next_cursor: null })),
      ),
    );
    renderApp({ path: "/players/owner" });

    const section = await screen.findByRole("region", { name: "Recent games" });
    expect(await within(section).findByText("No finished games yet")).toBeInTheDocument();
  });
});

describe("recent games on your own profile", () => {
  it("shows what each rated game did to your rating, and links to the full history", async () => {
    signedIn();
    myProfile([rating("blitz", 12, 1513)]);
    mswServer.use(
      http.get(url(`/players/${ME_ID}/matches`), () =>
        HttpResponse.json(
          envelope({
            entries: [match(1, ME_ID, RIVAL_ID, { before: 1500, after: 1513 })],
            next_cursor: null,
          }),
        ),
      ),
      http.get(url("/ratings/me/history"), () =>
        HttpResponse.json(
          envelope({
            variant: "russian_8x8",
            speed_class: "blitz",
            points: [],
            has_earlier: false,
          }),
        ),
      ),
    );
    renderApp({ path: "/profile" });

    const section = await screen.findByRole("region", { name: "Recent games" });
    const row = await within(section).findByRole("listitem");
    expect(within(row).getByText("Rating +13, now 1,513")).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "All games" })).toHaveAttribute(
      "href",
      "/games/history",
    );
  });

  it("offers a retry when the games could not be read", async () => {
    signedIn();
    myProfile([]);
    let calls = 0;
    mswServer.use(
      http.get(url(`/players/${ME_ID}/matches`), () => {
        calls += 1;
        return calls === 1
          ? HttpResponse.json(
              { code: "unavailable", message: "Down.", request_id: null, correlation_id: null },
              { status: 503 },
            )
          : HttpResponse.json(envelope({ entries: [], next_cursor: null }));
      }),
    );
    renderApp({ path: "/profile" });

    const section = await screen.findByRole("region", { name: "Recent games" });
    await userEvent.click(await within(section).findByRole("button", { name: /try again/i }));

    expect(await within(section).findByText("No finished games yet")).toBeInTheDocument();
  });
});

describe("rating progress", () => {
  const LINE = {
    variant: "russian_8x8",
    speed_class: "blitz",
    has_earlier: true,
    points: [
      { match_id: "m1", at: "2026-10-01T10:00:00Z", before: 1500, after: 1512, delta: 12 },
      { match_id: "m2", at: "2026-10-02T10:00:00Z", before: 1512, after: 1497, delta: -15 },
      { match_id: "m3", at: "2026-10-03T10:00:00Z", before: 1497, after: 1509, delta: 12 },
    ],
  };

  function withLine(line: typeof LINE, ratings = [rating("blitz", 12, 1509)]) {
    signedIn();
    myProfile(ratings);
    const asked: string[] = [];
    mswServer.use(
      http.get(url(`/players/${ME_ID}/matches`), () =>
        HttpResponse.json(envelope({ entries: [], next_cursor: null })),
      ),
      http.get(url("/ratings/me/history"), ({ request }) => {
        asked.push(new URL(request.url).searchParams.get("speed_class") ?? "");
        return HttpResponse.json(envelope(line));
      }),
    );
    return asked;
  }

  it("draws the stored points and reads out the latest, starting from the first game's rating before it", async () => {
    withLine(LINE);
    renderApp({ path: "/profile" });

    const section = await screen.findByRole("region", { name: "Rating progress" });
    const chart = await within(section).findByRole("img", { name: /from 1,500 to 1,509/ });
    expect(chart).toBeInTheDocument();
    expect(section.querySelector('[aria-live="polite"]')).toHaveTextContent("1,509");
    // `has_earlier` — the line does not pretend to start at game one.
    expect(within(section).getByText("Last 3 rated games")).toBeInTheDocument();
  });

  it("moves the readout with the arrow keys, as the pointer does", async () => {
    withLine(LINE);
    renderApp({ path: "/profile" });

    const section = await screen.findByRole("region", { name: "Rating progress" });
    const chart = await within(section).findByRole("img");
    chart.focus();
    await userEvent.keyboard("{ArrowLeft}");

    const readout = section.querySelector('[aria-live="polite"]');
    expect(readout).toHaveTextContent("1,497");
    expect(readout).toHaveTextContent("−15");
  });

  it("lists every point as a table, newest first", async () => {
    withLine(LINE);
    renderApp({ path: "/profile" });

    const section = await screen.findByRole("region", { name: "Rating progress" });
    await userEvent.click(await within(section).findByText("Show as a table"));
    const rows = within(within(section).getByRole("table")).getAllByRole("row");
    // A header row, then one per stored adjustment.
    expect(rows).toHaveLength(4);
    expect(within(rows[1] as HTMLElement).getByText("1,509")).toBeInTheDocument();
  });

  it("opens on the class with the most games and switches to another", async () => {
    const asked = withLine(LINE, [rating("blitz", 12, 1509), rating("rapid", 3, 1480)]);
    renderApp({ path: "/profile" });

    const section = await screen.findByRole("region", { name: "Rating progress" });
    await within(section).findByRole("img");
    await userEvent.click(within(section).getByRole("button", { name: "Rapid" }));

    expect(within(section).getByRole("button", { name: "Rapid" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(asked).toEqual(["blitz", "rapid"]);
  });

  it("says so, and asks for nothing, when no rated game has been played", async () => {
    const asked = withLine(LINE, [rating("blitz", 0)]);
    renderApp({ path: "/profile" });

    const section = await screen.findByRole("region", { name: "Rating progress" });
    expect(within(section).getByText("No rated games yet")).toBeInTheDocument();
    expect(asked).toEqual([]);
  });
});

describe("statistics", () => {
  it("does not state a highest rating nothing computes, and says how win rate counts draws", async () => {
    signedIn();
    myProfile([]);
    mswServer.use(
      http.get(url(`/players/${ME_ID}/matches`), () =>
        HttpResponse.json(envelope({ entries: [], next_cursor: null })),
      ),
    );
    renderApp({ path: "/profile" });

    const section = await screen.findByRole("region", { name: "Statistics" });
    expect(within(section).queryByText(/highest rating/i)).toBeNull();
    expect(
      within(section).getByText(
        "Win rate is wins divided by games played, so draws count as games.",
      ),
    ).toBeInTheDocument();
  });
});
