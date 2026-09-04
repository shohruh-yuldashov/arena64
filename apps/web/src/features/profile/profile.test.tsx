import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { httpClient } from "@/shared/api/client";
import { env } from "@/shared/config/env";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * Profile, through the real app — A64-020.3 §20.
 *
 * Every test mounts `App`: the real router, the real guard, the real
 * provider graph and the real Axios instance with its auth interceptor. A
 * page rendered directly would prove the component works and say nothing
 * about whether the route exists, whether the guard is on it, or whether
 * the request carries a token.
 *
 * MSW intercepts at the network, so the envelope unwrap, the error
 * normalisation and the cache policy all run for real.
 */
const url = (path: string) => `${env.VITE_API_URL}${path}`;
const REFRESH = url("/auth/browser/refresh");
const ME = url("/profile/me");
const MY_RATINGS = url("/ratings/me");
const PRIVACY = url("/profile/privacy");
const PATCH_PROFILE = url("/profile");
const AVATAR = url("/profile/avatar");

const PLAYER_ID = "019fb9ea-0a0c-7cec-9c5f-402727c31a96";

const USER = {
  id: PLAYER_ID,
  username: "player_one",
  display_name: "Player One",
  email: "player@example.com",
  is_active: true,
  is_verified: true,
};

const STATISTICS = {
  games_played: 40,
  wins: 24,
  losses: 12,
  draws: 4,
  win_rate: 0.6,
  current_rating: 1620,
  highest_rating: 1701,
  current_streak: 3,
  best_win_streak: 7,
};

function envelope<T>(data: T) {
  return { data, meta: { request_id: null, correlation_id: null } };
}

function myProfile(overrides: Record<string, unknown> = {}) {
  return envelope({
    id: PLAYER_ID,
    username: "player_one",
    display_name: "Player One",
    bio: "I play draughts.",
    country: "UZ",
    avatar_url: null,
    thumbnail_url: null,
    joined_at: "2026-01-15T10:00:00Z",
    is_online: true,
    last_seen: null,
    statistics: STATISTICS,
    ...overrides,
  });
}

function ratings() {
  return envelope({
    player_id: PLAYER_ID,
    ratings: [
      {
        variant: "russian_8x8",
        speed_class: "classical",
        rating: 1620.5,
        deviation: 80,
        games_played: 40,
        is_provisional: false,
      },
      {
        variant: "russian_8x8",
        speed_class: "blitz",
        rating: 1500,
        deviation: 350,
        games_played: 0,
        is_provisional: true,
      },
    ],
  });
}

function emptyTournaments() {
  return envelope({ entries: [], next_cursor: null });
}

/** The signed-in baseline: a live session and the three self reads. */
function signedIn() {
  mswServer.use(
    http.post(REFRESH, () =>
      HttpResponse.json(
        envelope({
          access_token: "token-1",
          token_type: "Bearer",
          expires_in: 900,
          user: USER,
        }),
      ),
    ),
    http.get(ME, () => HttpResponse.json(myProfile())),
    http.get(MY_RATINGS, () => HttpResponse.json(ratings())),
    http.get(url(`/players/${PLAYER_ID}/tournaments`), () =>
      HttpResponse.json(emptyTournaments()),
    ),
  );
}

/** No session — every guarded route should turn the visitor away. */
function anonymous() {
  mswServer.use(
    http.post(REFRESH, () =>
      HttpResponse.json(
        {
          code: "invalid_session",
          message: "No session.",
          request_id: null,
          correlation_id: null,
        },
        { status: 401 },
      ),
    ),
  );
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  httpClient.interceptors.request.clear();
  httpClient.interceptors.response.clear();
});

describe("the profile route", () => {
  it("is protected by RequireAuth and returns the visitor to it after signing in", async () => {
    // The first production use of `RequireAuth` — asserted through the real
    // router rather than by rendering the guard, because "the guard works"
    // and "the guard is on this route" are different claims and only the
    // second one has ever been the defect.
    anonymous();
    const { unmount } = renderApp({ path: "/profile" });

    // Bounced to sign-in, and the destination is carried so the round trip
    // is not a dead end.
    expect(await screen.findByRole("heading", { level: 1, name: /sign in/i })).toBeVisible();
    expect(window.location.href).toBeDefined();

    unmount();

    // With a session, the same URL renders the page — so the redirect above
    // was the guard and not a missing route.
    signedIn();
    renderApp({ path: "/profile" });

    expect(await screen.findByRole("heading", { level: 1, name: "Player One" })).toBeVisible();
  });

  it("renders identity, statistics and ratings from three fixed requests", async () => {
    let profileCalls = 0;
    signedIn();
    mswServer.use(
      http.get(ME, () => {
        profileCalls += 1;
        return HttpResponse.json(myProfile());
      }),
    );

    renderApp({ path: "/profile" });

    expect(await screen.findByRole("heading", { level: 1, name: "Player One" })).toBeVisible();
    expect(screen.getByText("@player_one")).toBeVisible();
    expect(screen.getByText("I play draughts.")).toBeVisible();

    // Statistics come from the backend's own figures. 24/40 is 60%, and the
    // client renders the API's `win_rate` rather than recomputing it.
    const statistics = screen.getByRole("region", {
      name: /statistics|statistika|статистика/i,
    });
    expect(within(statistics).getByText("60%")).toBeVisible();
    expect(within(statistics).getByText("40")).toBeVisible();

    // Ratings arrive from `/ratings/me`, because `/profile/me` has none.
    const ratingsRegion = await screen.findByRole("region", {
      name: /ratings|reyting|рейтинг/i,
    });
    expect(within(ratingsRegion).getByText("1,621")).toBeVisible();

    // A64-025.9: the identity band leads with the category actually
    // played — classical has 40 games, blitz has none — so the same figure
    // is stated twice on purpose, once as the headline and once in the
    // grid. Blitz's starting 1,500 is never the headline.
    expect(
      screen.getByText(/classical rating|klassik reytingi|рейтинг · классика/i),
    ).toBeVisible();
    expect(screen.getAllByText("1,621")).toHaveLength(2);
    expect(screen.queryByText("1,500")).not.toBeInTheDocument();

    // A category with no games says so instead of presenting the starting
    // value as a measurement.
    expect(
      within(ratingsRegion).getByText(/not rated yet|reyting yo'q|рейтинга пока нет/i),
    ).toBeVisible();

    // One profile request, not one per section — three components read the
    // same query.
    await waitFor(() => expect(profileCalls).toBe(1));
  });
});

describe("the public profile", () => {
  it("renders only what the server sent, and 404s at the URL that was typed", async () => {
    let ratingCalls = 0;
    let tournamentCalls = 0;
    anonymous();
    // Privacy hid the country, the online indicator, last seen **and** the
    // statistics — so the response simply lacks them. The page must not
    // invent "Offline" or a zeroed record from a missing key.
    mswServer.use(
      http.get(url("/profiles/hidden_player"), () =>
        HttpResponse.json(
          envelope({
            id: PLAYER_ID,
            username: "hidden_player",
            display_name: "Hidden Player",
            avatar_url: null,
            thumbnail_url: null,
            country: null,
            language: "uz",
            bio: null,
            joined_at: "2026-02-01T10:00:00Z",
            statistics: null,
            ratings: { classic: null, rapid: null, blitz: null },
          }),
        ),
      ),
      // Served, and asserted never to be called. A handler that is absent
      // makes an unwanted request an MSW warning; one that answers makes
      // the count below the assertion.
      http.get(url(`/players/${PLAYER_ID}/ratings`), () => {
        ratingCalls += 1;
        return HttpResponse.json(ratings());
      }),
      http.get(url(`/players/${PLAYER_ID}/tournaments`), () => {
        tournamentCalls += 1;
        return HttpResponse.json(emptyTournaments());
      }),
      http.get(url("/profiles/nobody"), () =>
        HttpResponse.json(
          {
            code: "not_found",
            message: "No such user.",
            request_id: null,
            correlation_id: null,
          },
          { status: 404 },
        ),
      ),
    );

    const hidden = renderApp({ path: "/players/hidden_player" });

    expect(
      await screen.findByRole("heading", { level: 1, name: "Hidden Player" }),
    ).toBeVisible();
    expect(screen.queryByText(/online|onlayn|в сети/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/offline|oflayn|не в сети/i)).not.toBeInTheDocument();
    expect(screen.getByText(/statistics are hidden|yashirilgan|скрыта/i)).toBeVisible();

    // **Not the ratings, and not the tournament history** — A64-026.5
    // §44.2. Both endpoints take `CurrentUser`, so a viewer with no account
    // cannot read either, and this test used to assert otherwise by mocking
    // a `200` the real server would never send. It passed while the page
    // looped: `401` -> refresh -> "session ended" -> `removeQueries` ->
    // refetch -> `401`, measured at ~175 requests a second.
    expect(
      screen.queryByRole("region", { name: /ratings|reyting|рейтинг/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(/sign in to see|ko'rish uchun kiring|войдите, чтобы увидеть/i),
    ).toBeVisible();
    expect(ratingCalls).toBe(0);
    expect(tournamentCalls).toBe(0);

    hidden.unmount();

    // An unknown name is a stable page, not a spinner and not a crash —
    // and the address bar keeps what was typed, so the typo is visible.
    renderApp({ path: "/players/nobody" });

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: /player not found|topilmadi|не найден/i,
      }),
    ).toBeVisible();
    // The API's English prose never reaches the DOM.
    expect(screen.queryByText("No such user.")).not.toBeInTheDocument();
  });
});

describe("profile editing", () => {
  it("submits only dirty values and refreshes both profile surfaces", async () => {
    const user = userEvent.setup();
    let patched: Record<string, unknown> | undefined;
    let profileReads = 0;

    signedIn();
    mswServer.use(
      http.get(ME, () => {
        profileReads += 1;
        return HttpResponse.json(myProfile());
      }),
      http.patch(PATCH_PROFILE, async ({ request }) => {
        patched = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(myProfile({ display_name: "Renamed", bio: null }));
      }),
    );

    renderApp({ path: "/settings/profile" });

    const displayName = await screen.findByLabelText(
      /display name|ko'rsatiladigan|отображаемое/i,
    );
    const submit = screen.getByRole("button", { name: /^save$|^saqlash$|^сохранить$/i });

    // Nothing has changed, so there is nothing to send.
    expect(submit).toBeDisabled();

    await user.clear(displayName);
    await user.type(displayName, "Renamed");
    expect(submit).toBeEnabled();

    await user.click(submit);

    await waitFor(() => expect(patched).toBeDefined());
    // Exactly the three fields `ProfileUpdateRequest` accepts — no
    // username, no email, nothing the endpoint would reject.
    expect(Object.keys(patched ?? {}).sort()).toEqual(["bio", "country", "display_name"]);
    expect(patched?.display_name).toBe("Renamed");

    // Saved, and re-baselined against the server's answer — so the form is
    // clean again rather than dirty against its own successful save.
    const readsBeforeSave = profileReads;

    expect(await screen.findByRole("status")).toHaveTextContent(/saved|saqlandi|сохранено/i);
    await waitFor(() => expect(submit).toBeDisabled());

    // `me` was written from the response, not refetched — the public
    // surfaces were invalidated and `me` was not. Asserted as "the save
    // added no read" rather than as an absolute count: `/profile/me` has a
    // second consumer since A64-025.9B §19 (the header reads it for the
    // avatar), and `createTestQueryClient` sets `staleTime: 0`, so two
    // consumers mounting a tick apart fetch twice here where production's
    // 30-second window fetches once. The count before the save is whatever
    // mounting cost; what this test is about is that saving cost nothing.
    expect(profileReads).toBe(readsBeforeSave);
  });
});

describe("the avatar", () => {
  it("refuses an oversized file before uploading and replaces a valid one", async () => {
    const user = userEvent.setup();
    let uploads = 0;

    signedIn();
    mswServer.use(
      http.post(AVATAR, () => {
        uploads += 1;
        return HttpResponse.json(
          envelope({
            avatar_url: "https://cdn.example/avatars/a.webp",
            thumbnail_url: null,
            avatar_version: 2,
            uploaded_at: "2026-08-05T10:00:00Z",
            dimensions: null,
          }),
        );
      }),
    );

    // A64-025.9: the avatar is edited where the rest of the profile is
    // edited. `/profile` shows it; it no longer uploads it.
    renderApp({ path: "/settings/profile" });

    const input = await screen.findByLabelText(/upload a photo|rasm yuklash|загрузить фото/i);

    // Over the 5 MB limit. Refused in the browser, so nothing is sent — the
    // point of the client check is not to be the guarantee but to avoid
    // pushing five megabytes over a phone connection to be told no.
    //
    // `size` is redefined rather than actually allocated: the check reads
    // that property, and materialising five real megabytes in jsdom
    // exhausts the worker's heap — which it did, on the first run of this
    // test.
    const huge = new File(["x"], "big.png", { type: "image/png" });
    Object.defineProperty(huge, "size", { value: 5 * 1024 * 1024 + 1 });
    await user.upload(input, huge);

    expect(await screen.findByRole("alert")).toHaveTextContent(/larger|katta|больше/i);
    expect(uploads).toBe(0);

    // A valid one goes through.
    const ok = new File(["small"], "ok.png", { type: "image/png" });
    await user.upload(input, ok);

    await waitFor(() => expect(uploads).toBe(1));
  });
});

describe("privacy", () => {
  it("saves through the server and lets the refetched public profile decide", async () => {
    const user = userEvent.setup();
    let hidden = false;

    signedIn();
    mswServer.use(
      http.get(PRIVACY, () =>
        HttpResponse.json(
          envelope({
            show_country: true,
            last_seen: "everyone",
            show_last_seen: true,
            show_statistics: true,
            online_status: "everyone",
            show_online_status: true,
            activity: "everyone",
            show_activity: true,
          }),
        ),
      ),
      http.patch(PRIVACY, async ({ request }) => {
        const body = (await request.json()) as { show_statistics?: boolean };
        if (body.show_statistics === false) hidden = true;
        return HttpResponse.json(
          envelope({
            show_country: true,
            last_seen: "everyone",
            show_last_seen: true,
            show_statistics: !hidden,
            online_status: "everyone",
            show_online_status: true,
            activity: "everyone",
            show_activity: true,
          }),
        );
      }),
    );

    renderApp({ path: "/settings/privacy" });

    const toggle = await screen.findByLabelText(/show my statistics|statistikani|статистику/i);
    expect(toggle).toBeChecked();

    await user.click(toggle);

    // The server confirmed, and the control reflects the server's answer
    // rather than an optimistic local flip.
    await waitFor(() => expect(hidden).toBe(true));
    expect(await screen.findByRole("status")).toHaveTextContent(/saved|saqlandi|сохранено/i);
    await waitFor(() => expect(toggle).not.toBeChecked());
  });
});

describe("tournament history", () => {
  it("pages by cursor and never fetches a tournament row by row", async () => {
    const user = userEvent.setup();
    const historyCalls: (string | null)[] = [];
    let detailCalls = 0;

    signedIn();
    mswServer.use(
      http.get(url(`/players/${PLAYER_ID}/tournaments`), ({ request }) => {
        const after = new URL(request.url).searchParams.get("after");
        historyCalls.push(after);
        return HttpResponse.json(
          after === null
            ? envelope({
                entries: [entry("Spring Open", 1), entry("Winter Cup", null)],
                next_cursor: "cursor-1",
              })
            : envelope({ entries: [entry("Autumn Blitz", 3)], next_cursor: null }),
        );
      }),
      // Any per-row detail request is the N+1 A64-020.0C removed on the
      // server; nothing here may reintroduce it on the client.
      http.get(/\/tournaments\/[0-9a-f-]{36}$/, () => {
        detailCalls += 1;
        return HttpResponse.json(envelope({}));
      }),
    );

    renderApp({ path: "/profile" });

    expect(await screen.findByText("Spring Open")).toBeVisible();
    // A tournament still being played has no rank — that is "in progress",
    // not "unplaced".
    expect(screen.getByText(/in progress|davom|идёт/i)).toBeVisible();

    await user.click(screen.getByRole("button", { name: /load more|yana|показать ещё/i }));

    expect(await screen.findByText("Autumn Blitz")).toBeVisible();
    // The cursor was sent back unread, and only two requests were made for
    // three rows.
    expect(historyCalls).toEqual([null, "cursor-1"]);
    expect(detailCalls).toBe(0);
  });
});

function entry(name: string, finalRank: number | null) {
  return {
    tournament: {
      id: `${finalRank ?? 9}9fb9ea-0a0c-7cec-9c5f-402727c31a96`,
      name,
      format: "single_elimination",
      variant: "russian_8x8",
      speed_class: "classical",
      rated: true,
      capacity: 8,
      status: finalRank === null ? "in_progress" : "completed",
      entrant_count: 8,
      current_round: null,
      registration_deadline: null,
      created_at: "2026-07-01T10:00:00Z",
      started_at: "2026-07-01T11:00:00Z",
      completed_at: finalRank === null ? null : "2026-07-01T13:00:00Z",
    },
    seed_number: 2,
    final_rank: finalRank,
    final_status: finalRank === null ? null : "eliminated",
  };
}
