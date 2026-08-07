import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";

import { httpClient } from "@/shared/api/client";
import { env } from "@/shared/config/env";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * Friend challenges, through the real app — A64-022.5 §22.
 *
 * Every test mounts `App`: the real router, both guards, the provider graph
 * and the live Axios instance. A page rendered directly would prove a
 * component works and say nothing about whether `/challenges` exists,
 * whether it is guarded, or whether its requests carry a token.
 *
 * The HTTP layer is the only thing substituted.
 */
const url = (path: string) => `${env.VITE_API_URL}${path}`;
const REFRESH = url("/auth/browser/refresh");
const TICKET = url("/auth/ws-ticket");
const INCOMING = url("/challenges/incoming");
const OUTGOING = url("/challenges/outgoing");
const PENDING = url("/matchmaking/matches/pending");
const QUEUE_ME = url("/matchmaking/queue/me");

const MATCH_ID = "019fe500-0000-7000-8000-0000000000b2";
const CHALLENGE_ID = "019fe500-0000-7000-8000-0000000000c3";

const VIEWER = {
  id: "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
  username: "viewer",
  display_name: "Viewer",
  email: "viewer@example.com",
  is_active: true,
  is_verified: true,
};

const RIVAL_ID = "019fb9ea-0a0c-7cec-9c5f-402727c31b01";

const envelope = <T,>(data: T) => ({ data, meta: { request_id: null, correlation_id: null } });

/** A deadline the local clock crosses while the page is mounted. */
const soon = () => new Date(Date.now() + 400).toISOString();

/** What `/profile` needs to render, and nothing more. */
const PROFILE = {
  ...VIEWER,
  avatar_url: null,
  thumbnail_url: null,
  country: null,
  language: "uz",
  bio: null,
  joined_at: "2026-01-01T10:00:00Z",
  ratings: { classic: null, rapid: null, blitz: null },
  statistics: null,
};
const cursorPage = <T,>(items: T[]) =>
  envelope({ items, page: { next_cursor: null, has_more: false } });

const CONTROLS = [
  { id: "blitz_3_2", label: "3+2", base_time_ms: 180_000, increment_ms: 2_000 },
  { id: "rapid_10_0", label: "10+0", base_time_ms: 600_000, increment_ms: 0 },
];

function player(overrides: Record<string, unknown> = {}) {
  return {
    id: RIVAL_ID,
    username: "rival",
    display_name: "Rival",
    avatar_url: null,
    thumbnail_url: null,
    country: null,
    language: "uz",
    bio: null,
    joined_at: "2026-01-01T10:00:00Z",
    relationship: "friend",
    is_online: null,
    last_seen: null,
    ratings: { classic: null, rapid: null, blitz: null },
    statistics: null,
    ...overrides,
  };
}

/** One challenge, expiring well into the future so no row reads "expired". */
function challenge(overrides: Record<string, unknown> = {}) {
  return {
    id: CHALLENGE_ID,
    status: "pending",
    player: player(),
    time_control_id: "blitz_3_2",
    variant: "russian_8x8",
    rated: false,
    created_at: "2026-08-07T10:00:00Z",
    expires_at: new Date(Date.now() + 3 * 3_600_000).toISOString(),
    responded_at: null,
    created_match_id: null,
    ...overrides,
  };
}

/** Every socket the app opened, so a test can push a frame into one. */
interface StubSocket {
  onmessage: ((event: { data: string }) => void) | null;
}

function stubWebSocket(): StubSocket[] {
  const sockets: StubSocket[] = [];
  vi.stubGlobal(
    "WebSocket",
    class {
      static readonly OPEN = 1;
      static readonly CONNECTING = 0;
      readyState = 0;
      onmessage: ((event: { data: string }) => void) | null = null;
      onclose: unknown = null;
      onerror: unknown = null;
      onopen: unknown = null;
      constructor() {
        sockets.push(this);
      }
      close() {}
      send() {}
    },
  );
  return sockets;
}

interface Backend {
  incoming: Record<string, unknown>[];
  outgoing: Record<string, unknown>[];
  incomingReads: number;
  outgoingReads: number;
  created: Record<string, unknown>[];
  matchAccepts: string[];
  /** What `POST /matchmaking/matches/{id}/accept` answers with. */
  matchStatus: "pending_acceptance" | "active";
}

function backend(overrides: Partial<Backend> = {}): Backend {
  return {
    incoming: [],
    outgoing: [],
    incomingReads: 0,
    outgoingReads: 0,
    created: [],
    matchAccepts: [],
    matchStatus: "active",
    ...overrides,
  };
}

function signedIn(state: Backend): void {
  mswServer.use(
    http.post(REFRESH, () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: VIEWER })),
    ),
    http.post(TICKET, () =>
      HttpResponse.json(envelope({ ticket: "t1", expires_at: "2026-08-07T10:00:30Z" }), {
        status: 201,
      }),
    ),
    http.get(url("/time-controls"), () => HttpResponse.json(envelope(CONTROLS))),
    http.get(INCOMING, () => {
      state.incomingReads += 1;
      return HttpResponse.json(cursorPage(state.incoming));
    }),
    http.get(OUTGOING, () => {
      state.outgoingReads += 1;
      return HttpResponse.json(cursorPage(state.outgoing));
    }),
    // The lobby's two reads. `404` is absence, which is what a player with
    // no queue ticket and no offer looks like.
    http.get(QUEUE_ME, () => new HttpResponse(null, { status: 404 })),
    http.get(PENDING, () => new HttpResponse(null, { status: 404 })),
    http.post(url("/challenges"), async ({ request }) => {
      state.created.push((await request.json()) as Record<string, unknown>);
      return HttpResponse.json(envelope(challenge()), { status: 201 });
    }),
    http.post(url(`/challenges/${CHALLENGE_ID}/accept`), () => {
      state.incoming = [];
      return HttpResponse.json(
        envelope(challenge({ status: "accepted", created_match_id: MATCH_ID })),
      );
    }),
    http.post(url(`/challenges/${CHALLENGE_ID}/decline`), () => {
      state.incoming = [];
      return HttpResponse.json(envelope(challenge({ status: "declined" })));
    }),
    http.delete(url(`/challenges/${CHALLENGE_ID}`), () => {
      state.outgoing = [];
      return new HttpResponse(null, { status: 204 });
    }),
    http.post(url(`/matchmaking/matches/${MATCH_ID}/accept`), () => {
      state.matchAccepts.push(MATCH_ID);
      return HttpResponse.json(
        envelope({
          match_id: MATCH_ID,
          status: state.matchStatus,
          you_accepted: true,
          opponent: { player_id: RIVAL_ID, username: "rival", display_name: "Rival" },
          rated: false,
          speed_class: "blitz",
          base_time_ms: 180_000,
          increment_ms: 2_000,
          acceptance_deadline: new Date(Date.now() + 30_000).toISOString(),
        }),
      );
    }),
  );
}

function anonymous(): void {
  mswServer.use(
    http.post(REFRESH, () =>
      HttpResponse.json(
        { code: "invalid_session", message: "No.", request_id: null, correlation_id: null },
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

it("guards /challenges and reaches it from the social navigation", async () => {
  // §2, §19. The guard first: an unsigned visitor must not reach a page
  // whose every request would take a `401` and look like an outage.
  anonymous();
  const anon = renderApp({ path: "/challenges" });
  expect(await screen.findByRole("heading", { level: 1, name: /sign in/i })).toBeVisible();
  anon.unmount();

  signedIn(backend());
  renderApp({ path: "/friends" });

  // And the entry point, because a route nothing links to is the
  // "implemented and reachable from nothing" failure this codebase keeps
  // finding.
  const nav = await screen.findByRole("navigation", { name: /social/i });
  const link = within(nav).getByRole("link", { name: /challenges/i });
  expect(link).toHaveAttribute("href", "/challenges");
});

it("lists incoming challenges with their terms", async () => {
  // §3. The clock comes from the **catalogue**, looked up by the stored
  // code — a challenge carries `blitz_3_2`, not two integers.
  signedIn(backend({ incoming: [challenge({ rated: true })] }));
  renderApp({ path: "/challenges" });

  const list = await screen.findByRole("list", { name: /incoming/i });
  const row = within(list).getByRole("listitem");
  expect(within(row).getByText(/Rival/)).toBeVisible();
  expect(within(row).getByText(/3\+2/)).toBeVisible();
  expect(within(row).getByText(/2h left/)).toBeVisible();
  expect(
    within(row).getByRole("button", { name: /accept the challenge from rival/i }),
  ).toBeVisible();
  expect(
    within(row).getByRole("button", { name: /decline the challenge from rival/i }),
  ).toBeVisible();
});

it("shows sent challenges under their own tab, with cancel", async () => {
  // §3. Two endpoints, and the tab chooses which. Nothing on the client
  // decides which side of a challenge the viewer is on.
  signedIn(backend({ outgoing: [challenge()] }));
  renderApp({ path: "/challenges" });

  // The empty state first, because the incoming tab is the default and it
  // must say something rather than render a bare list.
  expect(await screen.findByText(/no challenges waiting/i)).toBeVisible();

  await userEvent.click(screen.getByRole("tab", { name: /sent/i }));

  const list = await screen.findByRole("list", { name: /sent/i });
  const row = within(list).getByRole("listitem");
  expect(
    within(row).getByRole("button", { name: /cancel the challenge to rival/i }),
  ).toBeVisible();
  expect(within(row).queryByRole("button", { name: /accept/i })).toBeNull();
});

it("sends a challenge from the friends list and refuses a second submit", async () => {
  // §4, §5, §22. The opponent is **not** a choice: the dialog is opened
  // from a friend, so `recipient_id` is an id the server returned. The
  // clock has no default, so Send is disabled until one is picked.
  const state = backend();
  signedIn(state);
  mswServer.use(
    http.get(url("/friends"), () =>
      HttpResponse.json(
        cursorPage([{ player: player(), friends_since: "2026-01-01T10:00:00Z" }]),
      ),
    ),
    http.get(url("/friends/count"), () => HttpResponse.json(envelope({ total: 1 }))),
  );

  renderApp({ path: "/friends" });

  await userEvent.click(
    await screen.findByRole("button", { name: /challenge rival to a game/i }),
  );

  const dialog = await screen.findByRole("dialog");
  const send = within(dialog).getByRole("button", { name: /send challenge/i });
  expect(send).toBeDisabled();

  await userEvent.click(within(dialog).getByRole("radio", { name: "3+2" }));
  await userEvent.click(within(dialog).getByRole("radio", { name: /rated/i }));
  expect(send).toBeEnabled();

  // Two presses in a row. The second must not reach the API: the form is
  // disabled in flight and the dialog closes on success, and the backend's
  // own `uq_friend_challenge__live_pair` is the half that does not depend
  // on a client behaving.
  await userEvent.click(send);
  await userEvent.click(send);

  await waitFor(() => expect(state.created).toHaveLength(1));
  expect(state.created[0]).toEqual({
    recipient_id: RIVAL_ID,
    time_control_id: "blitz_3_2",
    variant: "russian_8x8",
    rated: true,
  });
});

it("accepts a challenge and lands in the game in one press", async () => {
  // §6, §7 — the phase's whole point. A64-022.3 leaves the match
  // `BILATERAL`, so Accept chains the challenge accept into the match
  // accept; the challenger is already in, so the navigation happens without
  // a second interaction and without anybody searching for the match.
  const state = backend({ incoming: [challenge()], matchStatus: "active" });
  signedIn(state);
  const { router } = renderApp({ path: "/challenges" });

  await userEvent.click(
    await screen.findByRole("button", { name: /accept the challenge from rival/i }),
  );

  await waitFor(() => expect(state.matchAccepts).toEqual([MATCH_ID]));
  // The board. Rendered by the real router, so this proves the handoff
  // reaches a route that exists.
  await waitFor(() => expect(router.state.location.pathname).toBe(`/games/${MATCH_ID}`));
});

it("waits for the opponent when they have not taken their seat", async () => {
  // The other half of §7: the match exists but the challenger is not in it
  // yet, so the **shared** offer dialog takes over rather than this feature
  // inventing a waiting screen.
  const state = backend({ incoming: [challenge()], matchStatus: "pending_acceptance" });
  signedIn(state);
  const { router } = renderApp({ path: "/challenges" });

  await userEvent.click(
    await screen.findByRole("button", { name: /accept the challenge from rival/i }),
  );

  await waitFor(() => expect(state.matchAccepts).toEqual([MATCH_ID]));
  // `MatchOfferDialog`, unchanged, saying what it says in the lobby.
  expect(await screen.findByRole("alertdialog")).toBeVisible();
  expect(router.state.location.pathname).toBe("/challenges");
});

it("removes a declined challenge and a cancelled one", async () => {
  // §8, §9. Both after the call succeeds: the list is invalidated and the
  // server's next answer is what removes the row. Splicing it out first
  // would leave a failed decline showing nothing at all.
  const state = backend({ incoming: [challenge()], outgoing: [challenge()] });
  signedIn(state);
  renderApp({ path: "/challenges" });

  await userEvent.click(
    await screen.findByRole("button", { name: /decline the challenge from rival/i }),
  );
  expect(await screen.findByText(/no challenges waiting/i)).toBeVisible();

  await userEvent.click(screen.getByRole("tab", { name: /sent/i }));
  await userEvent.click(
    await screen.findByRole("button", { name: /cancel the challenge to rival/i }),
  );
  expect(await screen.findByText(/you have not challenged anyone/i)).toBeVisible();
});

it("re-reads both lists when a challenge notification arrives", async () => {
  // §11. No new websocket: the `notification.created` frame the platform
  // already delivers is the wake-up, and the HTTP read is the authority.
  // Both lists, because the frame carries a type but not a side.
  const sockets = stubWebSocket();
  const state = backend();
  signedIn(state);
  renderApp({ path: "/challenges" });

  await waitFor(() => expect(state.incomingReads).toBeGreaterThan(0));
  await waitFor(() => expect(sockets.length).toBeGreaterThan(0));

  const before = { incoming: state.incomingReads, outgoing: state.outgoingReads };
  state.incoming = [challenge()];

  sockets[0]?.onmessage?.({
    data: JSON.stringify({
      v: 1,
      type: "notification.created",
      request_id: null,
      channel: "notifications",
      payload: {
        notification_id: "019fe400-0000-7000-8000-000000000009",
        type: "friend_challenge_received",
        created_at: "2026-08-07T10:00:00Z",
      },
    }),
  });

  await waitFor(() => expect(state.incomingReads).toBeGreaterThan(before.incoming));
  await waitFor(() => expect(state.outgoingReads).toBeGreaterThan(before.outgoing));
  expect(await screen.findByText(/Rival/)).toBeVisible();
});

it("navigates from a challenge notification to the challenge list", async () => {
  // §12. The backend's closed target model, resolved by the client mapper —
  // and the surface A64-022.4 had to point past now exists, so this is
  // `/challenges` rather than the `/friends` placeholder.
  signedIn(backend());
  mswServer.use(
    http.get(url("/notifications/unread-count"), () =>
      HttpResponse.json(envelope({ unread_count: 1 })),
    ),
    http.get(url("/notifications"), () =>
      HttpResponse.json(
        envelope({
          entries: [
            {
              id: "019fe400-0000-7000-8000-00000000000a",
              type: "friend_challenge_received",
              category: "social",
              actor: null,
              tournament: null,
              game: null,
              challenge: {
                challenge_id: CHALLENGE_ID,
                opponent: {
                  player_id: RIVAL_ID,
                  username: "rival",
                  display_name: "Rival",
                  thumbnail_url: null,
                },
                time_control_id: "blitz_3_2",
                variant: "russian_8x8",
                rated: false,
                expires_at: "2026-08-08T10:00:00Z",
                match_id: null,
              },
              target: { type: "challenges", ref: null },
              created_at: "2026-08-07T10:00:00Z",
              read_at: null,
              is_read: false,
            },
          ],
          next_cursor: null,
        }),
      ),
    ),
  );

  renderApp({ path: "/notifications" });

  const list = await screen.findByRole("list", { name: /notifications/i });
  expect(within(list).getByRole("link")).toHaveAttribute("href", "/challenges");
});

it("re-reads the lists when a row's countdown reaches zero", async () => {
  // A64-022.6 §11, §22.8. The countdown **asks**; the server answers.
  //
  // The row is served with a deadline a moment away, so the local clock
  // crosses it while the page is mounted. What must happen is a refetch —
  // and what must *not* is the client deciding the challenge is gone: the
  // second read is what removes it, which is why the backend stops serving
  // it between the two.
  const state = backend({ incoming: [challenge({ expires_at: soon() })] });
  signedIn(state);
  renderApp({ path: "/challenges" });

  expect(await screen.findByText(/Rival/)).toBeVisible();
  const before = state.incomingReads;

  // The sweep has since run, so the authoritative read no longer has it.
  state.incoming = [];

  await waitFor(() => expect(state.incomingReads).toBeGreaterThan(before), { timeout: 3000 });
  expect(await screen.findByText(/no challenges waiting/i)).toBeVisible();
});

it("reloads to whatever the server currently says", async () => {
  // §12, §22.9. No `localStorage`, no client-held challenge state: a fresh
  // mount runs the same read a first visit does, so a challenge answered on
  // another device is simply absent.
  const state = backend({ incoming: [challenge()] });
  signedIn(state);
  const first = renderApp({ path: "/challenges" });
  expect(await screen.findByText(/Rival/)).toBeVisible();
  first.unmount();

  // Accepted elsewhere between the two mounts.
  state.incoming = [];
  renderApp({ path: "/challenges" });

  expect(await screen.findByText(/no challenges waiting/i)).toBeVisible();
});

it("offers a pending match from a page that is not the lobby", async () => {
  // §13, §22.10 — the global handoff, and the reason it was worth moving.
  //
  // `/profile` mounts no matchmaking code of its own. Before A64-022.6 a
  // challenger reading it would learn nothing about a game they had a
  // ten-minute window to join; now `AppShell` renders the one offer dialog
  // in the app and it reaches them anywhere.
  const state = backend();
  signedIn(state);
  mswServer.use(
    http.get(PENDING, () =>
      HttpResponse.json(
        envelope({
          match_id: MATCH_ID,
          status: "pending_acceptance",
          you_accepted: false,
          opponent: { player_id: RIVAL_ID, username: "rival", display_name: "Rival" },
          rated: false,
          speed_class: "blitz",
          base_time_ms: 180_000,
          increment_ms: 2_000,
          acceptance_deadline: new Date(Date.now() + 30_000).toISOString(),
        }),
      ),
    ),
    http.get(url("/profile/me"), () => HttpResponse.json(envelope(PROFILE))),
  );

  renderApp({ path: "/profile" });

  expect(await screen.findByRole("alertdialog")).toBeVisible();
});

it("does not drag a player into a game they are already playing", async () => {
  // The hazard the §13 audit found, and the reason the shell does not
  // navigate on `active` alone.
  //
  // `GET /matchmaking/matches/pending` reports an **active** match with no
  // time window, so a player with a game in progress gets one on every
  // page. A shell that navigated on it would make `/profile` unreachable
  // for the whole match. It navigates only for a match it offered.
  const state = backend();
  signedIn(state);
  mswServer.use(
    http.get(PENDING, () =>
      HttpResponse.json(
        envelope({
          match_id: MATCH_ID,
          status: "active",
          you_accepted: true,
          opponent: { player_id: RIVAL_ID, username: "rival", display_name: "Rival" },
          rated: false,
          speed_class: "blitz",
          base_time_ms: 180_000,
          increment_ms: 2_000,
          acceptance_deadline: new Date(Date.now() + 30_000).toISOString(),
        }),
      ),
    ),
    http.get(url("/profile/me"), () => HttpResponse.json(envelope(PROFILE))),
  );

  const { router } = renderApp({ path: "/profile" });

  await waitFor(() => expect(state.incomingReads >= 0).toBe(true));
  // Given time to misbehave, and it does not.
  await new Promise((resolve) => setTimeout(resolve, 150));
  expect(router.state.location.pathname).toBe("/profile");
  expect(screen.queryByRole("alertdialog")).toBeNull();
});
