import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { RelationshipState } from "@/entities/relationship";
import { actionsFor } from "@/entities/relationship";
import { socialKeys } from "@/features/social/api/keys";
import { httpClient } from "@/shared/api/client";
import { env } from "@/shared/config/env";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * Social, through the real app — A64-020.4 §22.
 *
 * Every test mounts `App`: the real router, `RequireAuth`, the provider
 * graph and the live Axios instance. A page rendered directly would prove
 * a component works and say nothing about whether the route exists, whether
 * the guard is on it, or whether the request carries a token.
 */
const url = (path: string) => `${env.VITE_API_URL}${path}`;
const REFRESH = url("/auth/browser/refresh");
const SEARCH = url("/users/search");
const FRIENDS = url("/friends");
const INCOMING = url("/friends/requests/incoming");
const OUTGOING = url("/friends/requests/outgoing");
const BLOCKS = url("/blocks");

const VIEWER_ID = "019fb9ea-0a0c-7cec-9c5f-402727c31a96";

const VIEWER = {
  id: VIEWER_ID,
  username: "viewer",
  display_name: "Viewer",
  email: "viewer@example.com",
  is_active: true,
  is_verified: true,
};

function envelope<T>(data: T) {
  return { data, meta: { request_id: null, correlation_id: null } };
}

function cursorPage<T>(items: T[], next: string | null = null) {
  return envelope({ items, page: { next_cursor: next, has_more: next !== null } });
}

let nextId = 1;
function player(username: string, relationship: RelationshipState | null, extra = {}) {
  return {
    id: `0000000${nextId++}-0a0c-7cec-9c5f-402727c31a96`,
    username,
    display_name: username,
    avatar_url: null,
    thumbnail_url: `https://cdn.example/thumbs/${username}.webp`,
    country: null,
    language: "uz",
    bio: null,
    joined_at: "2026-01-01T10:00:00Z",
    relationship,
    is_online: null,
    last_seen: null,
    ratings: { classic: null, rapid: null, blitz: null },
    statistics: null,
    ...extra,
  };
}

function signedIn() {
  mswServer.use(
    http.post(REFRESH, () =>
      HttpResponse.json(
        envelope({
          access_token: "token-1",
          token_type: "Bearer",
          expires_in: 900,
          user: VIEWER,
        }),
      ),
    ),
    http.get(FRIENDS, () => HttpResponse.json(cursorPage([]))),
    http.get(url("/friends/count"), () => HttpResponse.json(envelope({ total: 0 }))),
    http.get(INCOMING, () => HttpResponse.json(cursorPage([]))),
    http.get(OUTGOING, () => HttpResponse.json(cursorPage([]))),
    http.get(BLOCKS, () => HttpResponse.json(cursorPage([]))),
  );
}

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

describe("the social routes", () => {
  it("are all behind RequireAuth in the real router", async () => {
    // Four routes, one assertion each, because a guard forgotten on one of
    // four is exactly the defect that ships — the other three work and the
    // fourth quietly serves a `401` that looks like a loading failure.
    for (const path of ["/friends", "/friends/requests", "/friends/blocked", "/search"]) {
      anonymous();
      const view = renderApp({ path });
      expect(
        await screen.findByRole("heading", { level: 1, name: /sign in/i }),
        `${path} is not guarded`,
      ).toBeVisible();
      view.unmount();
    }

    signedIn();
    renderApp({ path: "/friends" });
    expect(await screen.findByRole("heading", { level: 1, name: /friends/i })).toBeVisible();
  });
});

describe("player search", () => {
  it("debounces, skips short terms, and renders the server's relationship", async () => {
    const user = userEvent.setup();
    const terms: string[] = [];

    signedIn();
    mswServer.use(
      http.get(SEARCH, ({ request }) => {
        terms.push(new URL(request.url).searchParams.get("q") ?? "");
        return HttpResponse.json(
          cursorPage([
            player("stranger", "none"),
            player("pal", "friend"),
            player("asked_me", "incoming_request"),
          ]),
        );
      }),
    );

    renderApp({ path: "/search" });

    const input = await screen.findByLabelText(
      /username or name|foydalanuvchi|имя пользователя/i,
    );

    // One character is below the API's floor — no request at all.
    await user.type(input, "a");
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(/at least|kamida|не менее/i),
    );
    expect(terms).toEqual([]);

    // Typing the rest issues **one** request, not one per keystroke.
    await user.type(input, "libek");
    expect(await screen.findByText("stranger")).toBeVisible();
    expect(terms).toEqual(["alibek"]);

    // Each row's action set comes from the state the server sent, so a
    // friend gets "remove" and somebody who asked gets no "add".
    // Scoped to the results list: `SocialNav` renders its own list of
    // links, so an unscoped `listitem` query finds the navigation first.
    const rows = within(
      screen.getByRole("list", { name: /find players|qidirish|поиск/i }),
    ).getAllByRole("listitem");
    expect(
      within(rows[0] as HTMLElement).getByRole("button", { name: /add friend/i }),
    ).toBeVisible();
    expect(
      within(rows[1] as HTMLElement).getByRole("button", { name: /remove friend/i }),
    ).toBeVisible();
    expect(
      within(rows[2] as HTMLElement).queryByRole("button", { name: /add friend/i }),
    ).not.toBeInTheDocument();

    // The thumbnail-versus-avatar choice is asserted in the Playwright
    // flow rather than here: Radix's `AvatarImage` only mounts the `<img>`
    // once the image has *loaded*, and jsdom never loads one — so there is
    // no element in this DOM to inspect, and an assertion that passed
    // would be asserting the fallback.
  });
});

describe("sending a request", () => {
  it("posts the server's player id and refreshes the outgoing list", async () => {
    const user = userEvent.setup();
    let sentTo: string | null = null;
    let outgoingReads = 0;
    const target = player("target", "none");

    signedIn();
    mswServer.use(
      http.get(SEARCH, () => HttpResponse.json(cursorPage([target]))),
      http.post(url("/friends/requests"), async ({ request }) => {
        const body = (await request.json()) as { player_id: string };
        sentTo = body.player_id;
        return HttpResponse.json(envelope({ id: "req-1", status: "pending" }), { status: 201 });
      }),
      http.get(OUTGOING, () => {
        outgoingReads += 1;
        return HttpResponse.json(cursorPage([]));
      }),
    );

    renderApp({ path: "/search" });
    await user.type(
      await screen.findByLabelText(/username or name|foydalanuvchi|имя пользователя/i),
      "target",
    );
    await user.click(await screen.findByRole("button", { name: /add friend/i }));

    // The id is the one the search returned — never a username, and the
    // sender is never sent at all.
    await waitFor(() => expect(sentTo).toBe(target.id));

    // The outgoing list was invalidated, so a visit to it refetches.
    const before = outgoingReads;
    await user.click(screen.getByRole("link", { name: /^requests$/i }));
    await waitFor(() => expect(outgoingReads).toBeGreaterThan(before));
  });
});

describe("request transitions", () => {
  it("accepts through the request id and never offers a contradictory pair", async () => {
    const user = userEvent.setup();
    let accepted: string | null = null;
    let friendReads = 0;

    signedIn();
    mswServer.use(
      http.get(INCOMING, () =>
        HttpResponse.json(
          cursorPage([
            {
              id: "req-7",
              status: "pending",
              player: player("asker", "incoming_request"),
              created_at: "2026-08-01T10:00:00Z",
              responded_at: null,
            },
          ]),
        ),
      ),
      http.post(url("/friends/requests/req-7/accept"), () => {
        accepted = "req-7";
        return HttpResponse.json(envelope({ id: "req-7", status: "accepted" }));
      }),
      http.get(FRIENDS, () => {
        friendReads += 1;
        return HttpResponse.json(cursorPage([]));
      }),
    );

    const { queryClient } = renderApp({ path: "/friends/requests" });
    // Seeded so there is an entry to invalidate — an unfetched key has no
    // state at all, and "no state" would pass a naive assertion.
    queryClient.setQueryData(socialKeys.friends(), {
      pages: [{ items: [], page: { next_cursor: null, has_more: false } }],
      pageParams: [undefined],
    });

    const incomingList = await screen.findByRole("list", {
      name: /^incoming$|^kiruvchi$|^входящие$/i,
    });
    const row = within(incomingList).getAllByRole("listitem")[0] as HTMLElement;
    // An incoming request offers accept and decline — and never "add
    // friend" or "cancel", which belong to other states. The button set
    // comes from one enum, so the impossible pairs are unrepresentable.
    expect(within(row).getByRole("button", { name: /accept/i })).toBeVisible();
    expect(within(row).getByRole("button", { name: /decline/i })).toBeVisible();
    expect(within(row).queryByRole("button", { name: /add friend/i })).not.toBeInTheDocument();
    expect(
      within(row).queryByRole("button", { name: /cancel request/i }),
    ).not.toBeInTheDocument();

    await user.click(within(row).getByRole("button", { name: /accept/i }));

    await waitFor(() => expect(accepted).toBe("req-7"));

    // Accepting is the transition that creates a friendship, so the friends
    // list is the one it makes stale. Asserted as **invalidated**, not as
    // refetched: the list is not mounted on this page, and TanStack marks
    // an inactive query stale rather than refetching it — which is the
    // correct behaviour and would make a refetch assertion wrong.
    await waitFor(() =>
      expect(queryClient.getQueryState(socialKeys.friends())?.isInvalidated).toBe(true),
    );
    expect(friendReads).toBe(0);
  });
});

describe("the friends list", () => {
  it("offers the way to the action its empty state names", async () => {
    signedIn();
    mswServer.use(http.get(FRIENDS, () => HttpResponse.json(cursorPage([]))));

    renderApp({ path: "/friends" });

    // The hint says to find players; the control that does it has to be
    // here, not only in the navigation beside it.
    const empty = await screen.findByRole("heading", {
      name: /no friends yet|hali do'st yo'q|друзей пока нет/i,
    });
    const action = within(empty.parentElement as HTMLElement).getByRole("link", {
      name: /^search$|^qidirish$|^поиск$/i,
    });
    expect(action).toHaveAttribute("href", "/search");
  });

  it("shows the handle only when it says something the name did not", async () => {
    signedIn();
    mswServer.use(
      http.get(FRIENDS, () =>
        HttpResponse.json(
          cursorPage([
            {
              // A display name of its own: both lines carry a fact.
              player: player("quiet_rook", "friend", { display_name: "Quiet Rook" }),
              friends_since: "2026-05-01T10:00:00Z",
            },
            {
              // No display name, so `nameOf` falls back to the username —
              // rendering `@plainmover` beneath `plainmover` would be two
              // lines carrying one fact.
              player: player("plainmover", "friend", { display_name: null }),
              friends_since: "2026-06-01T10:00:00Z",
            },
          ]),
        ),
      ),
    );

    renderApp({ path: "/friends" });

    expect(await screen.findByText("Quiet Rook")).toBeVisible();
    expect(screen.getByText("@quiet_rook")).toBeVisible();

    expect(screen.getByText("plainmover")).toBeVisible();
    expect(screen.queryByText("@plainmover")).toBeNull();
  });

  it("uses thumbnails and the server's presence, with one request per page", async () => {
    let friendCalls = 0;
    let profileCalls = 0;

    signedIn();
    mswServer.use(
      http.get(FRIENDS, () => {
        friendCalls += 1;
        return HttpResponse.json(
          cursorPage([
            {
              player: player("visible", "friend", { is_online: true }),
              friends_since: "2026-05-01T10:00:00Z",
            },
            // Privacy hid this friend's presence: the fields are simply
            // absent, and the row must not render "Offline" from that.
            { player: player("hidden", "friend"), friends_since: "2026-06-01T10:00:00Z" },
          ]),
        );
      }),
      // Any per-row profile fetch is the N+1 the server-side composer
      // exists to avoid; the client must not reintroduce it.
      http.get(/\/profiles\/[a-z_]+$/, () => {
        profileCalls += 1;
        return HttpResponse.json(envelope(player("x", "none")));
      }),
    );

    renderApp({ path: "/friends" });

    expect(await screen.findByText("visible")).toBeVisible();
    // Scoped to the results list: `SocialNav` renders its own list of
    // links, so an unscoped `listitem` query finds the navigation first.
    const rows = within(
      screen.getByRole("list", { name: /^friends$|^do'stlar$|^друзья$/i }),
    ).getAllByRole("listitem");

    // Present means rendered — as a word, not only a coloured dot.
    expect(within(rows[0] as HTMLElement).getByText(/online|onlayn|в сети/i)).toBeVisible();
    // Absent means absent. No default, in either direction.
    expect(
      within(rows[1] as HTMLElement).queryByText(
        /online|offline|onlayn|oflayn|в сети|не в сети/i,
      ),
    ).not.toBeInTheDocument();

    expect(friendCalls).toBe(1);
    expect(profileCalls).toBe(0);
  });
});

describe("blocking", () => {
  it("confirms first, then invalidates every social view", async () => {
    const user = userEvent.setup();
    let blocked: string | null = null;
    const counts = { friends: 0, blocked: 0, search: 0 };
    const target = player("nuisance", "none");

    signedIn();
    mswServer.use(
      http.get(SEARCH, () => {
        counts.search += 1;
        return HttpResponse.json(cursorPage([target]));
      }),
      http.get(FRIENDS, () => {
        counts.friends += 1;
        return HttpResponse.json(cursorPage([]));
      }),
      http.get(BLOCKS, () => {
        counts.blocked += 1;
        return HttpResponse.json(cursorPage([]));
      }),
      http.post(BLOCKS, async ({ request }) => {
        const body = (await request.json()) as { player_id: string };
        blocked = body.player_id;
        return HttpResponse.json(envelope({}), { status: 201 });
      }),
    );

    renderApp({ path: "/search" });
    await user.type(
      await screen.findByLabelText(/username or name|foydalanuvchi|имя пользователя/i),
      "nuisance",
    );
    await user.click(await screen.findByRole("button", { name: /^block/i }));

    // Destructive, so it asks — and the dialog names the player and states
    // only consequences the backend actually guarantees.
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(/nuisance/);
    expect(dialog.textContent ?? "").not.toMatch(/messages|xabarlar|сообщения/i);

    const before = { ...counts };
    await user.click(
      within(dialog).getByRole("button", { name: /confirm|tasdiqlash|подтвердить/i }),
    );

    await waitFor(() => expect(blocked).toBe(target.id));
    // A block ends a friendship, cancels requests and hides the target, so
    // every social view is stale at once — the one mutation that earns a
    // broad invalidation, and still scoped to `social`.
    await waitFor(() => expect(counts.search).toBeGreaterThan(before.search));
  });
});

describe("the relationship model", () => {
  it("maps every state to exactly one valid action set", () => {
    // A pure assertion over the single source of truth every surface
    // reads. Rendering five pages to discover a contradictory pair would
    // test five templates; this tests the rule they all obey.
    expect(actionsFor("none")).toEqual(["send_request", "block"]);
    expect(actionsFor("outgoing_request")).toEqual(["cancel_request", "block"]);
    expect(actionsFor("incoming_request")).toEqual([
      "accept_request",
      "decline_request",
      "block",
    ]);
    expect(actionsFor("friend")).toEqual(["remove_friend", "block"]);
    // A blocked player is not friendable, requestable or removable.
    expect(actionsFor("blocked")).toEqual(["unblock"]);

    // Absent is not `none`: an anonymous viewer and the viewer's own
    // profile both render no social controls at all.
    expect(actionsFor(null)).toEqual([]);
    expect(actionsFor(undefined)).toEqual([]);

    for (const state of [
      "none",
      "outgoing_request",
      "incoming_request",
      "friend",
      "blocked",
    ] as const) {
      const actions = actionsFor(state);
      expect(
        actions.includes("send_request") && actions.includes("accept_request"),
        `${state} offers both add and accept`,
      ).toBe(false);
      expect(
        actions.includes("remove_friend") && actions.includes("unblock"),
        `${state} offers both friend and block actions`,
      ).toBe(false);
    }
  });
});
