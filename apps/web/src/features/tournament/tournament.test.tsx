import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";

import { httpClient } from "@/shared/api/client";
import { env } from "@/shared/config/env";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * The tournament UI, through the real app — A64-020.6 §29, §30.
 *
 * Every test mounts the **real router** at a real path, so route
 * registration, the guard, the query layer, the mutations and the links to
 * `/games/…` are exercised together. §30 is explicit that an isolated
 * tournament-card test is insufficient; what is substituted here is the
 * HTTP layer and nothing else.
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

const OPEN_ID = "019fe100-0000-7000-8000-000000000001";
const DONE_ID = "019fe100-0000-7000-8000-000000000002";
const RIVAL = "019fe200-0000-7000-8000-00000000000b";
const THIRD = "019fe200-0000-7000-8000-00000000000c";
const LIVE_MATCH = "019fe300-0000-7000-8000-0000000000a1";
const PLAYED_MATCH = "019fe300-0000-7000-8000-0000000000a2";

function tournament(overrides: Record<string, unknown> = {}) {
  return {
    id: OPEN_ID,
    name: "Sunday Open",
    format: "single_elimination",
    variant: "russian_8x8",
    speed_class: "classical",
    rated: true,
    capacity: 4,
    status: "registration_open",
    entrant_count: 2,
    current_round: null,
    registration_deadline: "2026-08-09T18:00:00Z",
    created_at: "2026-08-01T10:00:00Z",
    started_at: null,
    completed_at: null,
    ...overrides,
  };
}

const PARTICIPANTS = [
  {
    player_id: VIEWER.id,
    username: "viewer",
    display_name: "Viewer",
    avatar_thumbnail_url: null,
  },
  { player_id: RIVAL, username: "rival", display_name: "Rival", avatar_thumbnail_url: null },
  { player_id: THIRD, username: "third", display_name: "Third", avatar_thumbnail_url: null },
];

/**
 * A four-slot bracket exercising every node state §29.5 lists.
 *
 * Round 1: one **bye** (Third advanced, no opponent) and one **live** pair.
 * Round 2: the final, holding the bye's winner and **waiting** for the
 * other semi-final — which is the node the backend used to report as a bye
 * and which must render as "waiting for an opponent".
 */
function bracket() {
  return {
    tournament_id: OPEN_ID,
    participants: PARTICIPANTS,
    rounds: [
      {
        round_number: 1,
        status: "in_progress",
        nodes: [
          {
            pairing_id: "019fe400-0000-7000-8000-000000000001",
            round_number: 1,
            slot: 0,
            light_player_id: VIEWER.id,
            dark_player_id: RIVAL,
            light_seed: 1,
            dark_seed: 4,
            winner_id: null,
            advancement_reason: null,
            is_bye: false,
            attempts: [
              {
                attempt_number: 1,
                match_id: LIVE_MATCH,
                light_player_id: VIEWER.id,
                dark_player_id: RIVAL,
                status: "created",
                outcome: null,
                winner_id: null,
              },
            ],
          },
          {
            pairing_id: "019fe400-0000-7000-8000-000000000002",
            round_number: 1,
            slot: 1,
            light_player_id: THIRD,
            dark_player_id: null,
            light_seed: 2,
            dark_seed: null,
            winner_id: THIRD,
            advancement_reason: "bye",
            is_bye: true,
            attempts: [],
          },
        ],
      },
      {
        round_number: 2,
        status: "pending",
        nodes: [
          {
            pairing_id: "019fe400-0000-7000-8000-000000000003",
            round_number: 2,
            slot: 0,
            light_player_id: THIRD,
            dark_player_id: null,
            light_seed: 2,
            dark_seed: null,
            winner_id: null,
            advancement_reason: null,
            // The server now says `false` here — this is the field the
            // `fix(tournament)` commit corrected. The client does not read
            // it at all, and this fixture proves the rendering does not
            // depend on it either.
            is_bye: false,
            attempts: [],
          },
        ],
      },
    ],
  };
}

function completedBracket() {
  return {
    tournament_id: DONE_ID,
    participants: PARTICIPANTS,
    rounds: [
      {
        round_number: 1,
        status: "completed",
        nodes: [
          {
            pairing_id: "019fe500-0000-7000-8000-000000000001",
            round_number: 1,
            slot: 0,
            light_player_id: VIEWER.id,
            dark_player_id: RIVAL,
            light_seed: 1,
            dark_seed: 2,
            winner_id: VIEWER.id,
            advancement_reason: "played",
            is_bye: false,
            attempts: [
              {
                attempt_number: 1,
                match_id: PLAYED_MATCH,
                light_player_id: VIEWER.id,
                dark_player_id: RIVAL,
                status: "completed",
                outcome: "decisive",
                winner_id: VIEWER.id,
              },
            ],
          },
        ],
      },
    ],
  };
}

/** Two players tied on third — the non-dense rank §29.7 is about. */
function standings() {
  return {
    tournament_id: DONE_ID,
    participants: PARTICIPANTS,
    standings: [
      {
        player_id: VIEWER.id,
        final_rank: 1,
        seed_number: 1,
        wins: 2,
        losses: 0,
        draws: 0,
        adjudicated_advancements: 0,
        final_status: "champion",
        elimination_round: null,
        eliminated_by_player_id: null,
      },
      {
        player_id: RIVAL,
        final_rank: 3,
        seed_number: 2,
        wins: 0,
        losses: 1,
        draws: 0,
        adjudicated_advancements: 0,
        final_status: "eliminated",
        elimination_round: 1,
        eliminated_by_player_id: VIEWER.id,
      },
      {
        player_id: THIRD,
        final_rank: 3,
        seed_number: 3,
        wins: 0,
        losses: 1,
        draws: 0,
        adjudicated_advancements: 0,
        final_status: "eliminated",
        elimination_round: 1,
        eliminated_by_player_id: VIEWER.id,
      },
    ],
  };
}

let requests: string[] = [];
let registrationWrites = 0;

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  httpClient.interceptors.request.clear();
  httpClient.interceptors.response.clear();
  requests = [];
  registrationWrites = 0;
  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: VIEWER })),
    ),
    // Any per-participant profile read is a failure of §26 — identities
    // arrive composed on the bracket and standings responses.
    http.get(url("/users/:id"), () => {
      requests.push("profile");
      return HttpResponse.json(envelope({}));
    }),
    http.get(url("/profiles/:username"), () => {
      requests.push("profile");
      return HttpResponse.json(envelope({}));
    }),
  );
});

function serveLobby(
  entries: ReturnType<typeof tournament>[],
  nextCursor: string | null = null,
) {
  mswServer.use(
    http.get(url("/tournaments"), ({ request }) => {
      const after = new URL(request.url).searchParams.get("after");
      const status = new URL(request.url).searchParams.get("status");
      requests.push(`list:${status ?? "all"}:${after ?? "first"}`);
      if (after !== null) {
        return HttpResponse.json(
          envelope({
            entries: [tournament({ id: DONE_ID, name: "Older Open" })],
            next_cursor: null,
          }),
        );
      }
      const filtered =
        status === null ? entries : entries.filter((entry) => entry.status === status);
      return HttpResponse.json(envelope({ entries: filtered, next_cursor: nextCursor }));
    }),
  );
}

function serveDetail(
  detail: ReturnType<typeof tournament>,
  options: {
    bracketBody?: unknown;
    standingsBody?: unknown;
    registration?: unknown;
  } = {},
) {
  mswServer.use(
    http.get(url(`/tournaments/${detail.id}`), () => {
      requests.push("detail");
      return HttpResponse.json(envelope(detail));
    }),
    http.get(url(`/tournaments/${detail.id}/bracket`), () => {
      requests.push("bracket");
      return HttpResponse.json(envelope(options.bracketBody ?? bracket()));
    }),
    http.get(url(`/tournaments/${detail.id}/standings`), () => {
      requests.push("standings");
      return HttpResponse.json(
        envelope(
          options.standingsBody ?? {
            tournament_id: detail.id,
            standings: [],
            participants: [],
          },
        ),
      );
    }),
    http.get(url(`/tournaments/${detail.id}/registrations/me`), () => {
      requests.push("registration");
      if (options.registration === undefined) {
        return HttpResponse.json(
          { code: "not_found", message: "no entry", request_id: null, correlation_id: null },
          { status: 404 },
        );
      }
      return HttpResponse.json(envelope(options.registration));
    }),
  );
}

it("lists tournaments, narrows them through the server, and pages by cursor", async () => {
  // §29.1 and §29.9 together. The filter assertion is the one that matters:
  // a client-side filter over one loaded page would pass a rendering check
  // while silently hiding open tournaments that sat on page two.
  serveLobby(
    [tournament(), tournament({ id: DONE_ID, name: "Winter Cup", status: "completed" })],
    "cursor-2",
  );
  const user = userEvent.setup();
  renderApp({ path: "/tournaments" });

  const list = await screen.findByRole(
    "list",
    { name: /tournaments|turnirlar/i },
    { timeout: 3000 },
  );
  await waitFor(() => expect(within(list).getAllByRole("listitem")).toHaveLength(2));
  expect(requests.filter((entry) => entry.startsWith("list:"))).toEqual(["list:all:first"]);

  // The status filter is sent to the server, not applied here.
  await user.click(screen.getByRole("radio", { name: /registration open|ro'yxat ochiq/i }));
  await waitFor(() => expect(requests).toContain("list:registration_open:first"));

  await user.click(screen.getByRole("radio", { name: /^all$|hammasi/i }));
  await user.click(await screen.findByRole("button", { name: /load more|yana yuklash/i }));

  // The opaque cursor, sent back verbatim — never decoded, never rebuilt.
  await waitFor(() => expect(requests).toContain("list:all:cursor-2"));
  expect(requests.filter((entry) => entry === "profile")).toHaveLength(0);
});

it("shows the authoritative participant state rather than inferring it", async () => {
  // §29.2 and §29.9. "Not registered" comes from a `404` on the viewer's own
  // entry — the endpoint this phase added — and not from the absence of a
  // button, which is the inversion §8 forbids.
  serveDetail(tournament());
  renderApp({ path: `/tournaments/${OPEN_ID}` });

  expect(
    await screen.findByText(/you have not entered|yozilmagansiz/i, undefined, {
      timeout: 3000,
    }),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: /enter tournament|turnirga yozilish/i }),
  ).toBeEnabled();
  expect(screen.queryByRole("button", { name: /^withdraw$|^chiqish$/i })).toBeNull();

  // One read per surface, and none per participant.
  expect(requests.filter((entry) => entry === "detail")).toHaveLength(1);
  expect(requests.filter((entry) => entry === "bracket")).toHaveLength(1);
  expect(requests.filter((entry) => entry === "registration")).toHaveLength(1);
  expect(requests.filter((entry) => entry === "profile")).toHaveLength(0);
  // A tournament that has not completed is never asked for standings.
  expect(requests.filter((entry) => entry === "standings")).toHaveLength(0);
});

it("registers once and shows the state the server confirmed", async () => {
  // §29.3. Two assertions, and the second is the point: a double-click must
  // not become two entries, because the second would be a `409` the player
  // caused and has to be told about.
  serveDetail(tournament());
  mswServer.use(
    http.post(url(`/tournaments/${OPEN_ID}/registrations`), async () => {
      registrationWrites += 1;
      await new Promise((resolve) => setTimeout(resolve, 30));
      return HttpResponse.json(
        envelope({
          tournament_id: OPEN_ID,
          player_id: VIEWER.id,
          status: "registered",
          registered_at: "2026-08-05T12:00:00Z",
          withdrawn_at: null,
          seed_number: null,
          tournament_status: "registration_open",
        }),
        { status: 201 },
      );
    }),
  );
  const user = userEvent.setup();
  renderApp({ path: `/tournaments/${OPEN_ID}` });

  const enter = await screen.findByRole(
    "button",
    { name: /enter tournament|turnirga yozilish/i },
    { timeout: 3000 },
  );
  await user.dblClick(enter);

  expect(
    await screen.findByText(/you are entered|siz yozilgansiz/i, undefined, { timeout: 3000 }),
  ).toBeVisible();
  expect(registrationWrites).toBe(1);
  // Server-confirmed, not optimistic: the withdraw control appears because
  // the response said `registered`, and it is the response that is cached.
  expect(screen.getByRole("button", { name: /^withdraw$|^chiqish$/i })).toBeVisible();
});

it("asks before withdrawing and reports the server's refusal by code", async () => {
  // §29.4 and §21. The refusal is rendered from `registration_not_open`,
  // never from the server's English sentence.
  serveDetail(tournament(), {
    registration: {
      tournament_id: OPEN_ID,
      player_id: VIEWER.id,
      status: "registered",
      registered_at: "2026-08-05T12:00:00Z",
      withdrawn_at: null,
      seed_number: 1,
      tournament_status: "registration_open",
    },
  });
  mswServer.use(
    http.delete(url(`/tournaments/${OPEN_ID}/registrations/me`), () =>
      HttpResponse.json(
        {
          code: "registration_not_open",
          message: "internal detail nobody should read",
          request_id: null,
          correlation_id: null,
        },
        { status: 409 },
      ),
    ),
  );
  const user = userEvent.setup();
  renderApp({ path: `/tournaments/${OPEN_ID}` });

  await user.click(
    await screen.findByRole("button", { name: /^withdraw$|^chiqish$/i }, { timeout: 3000 }),
  );

  // A confirmation, and one that promises only what the backend does.
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/enter again|qayta yozilishingiz/i)).toBeVisible();
  expect(within(dialog).queryByText(/rating|reyting|refund/i)).toBeNull();

  await user.click(within(dialog).getByRole("button", { name: /yes, withdraw|ha, chiqaman/i }));

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/entries are not open|ro'yxatga olish ochiq emas/i);
  expect(alert).not.toHaveTextContent(/internal detail/i);
});

it("renders bye, waiting, live and finished nodes as four different things", async () => {
  // §29.5 and §29.6 — the heart of the phase. A bye and a node waiting for
  // its opponent both show one name and one blank, and rendering them the
  // same is the confusion the backend's own `is_bye` had until this phase.
  serveDetail(tournament({ status: "in_progress", current_round: 1 }));
  renderApp({ path: `/tournaments/${OPEN_ID}` });

  const bracketRegion = await screen.findByRole(
    "region",
    { name: /bracket rounds|setka bosqichlari/i },
    { timeout: 3000 },
  );
  const nodes = within(bracketRegion).getAllByRole("listitem");
  expect(nodes).toHaveLength(3);

  // The bye says so in words, and names who advanced — never by colour.
  expect(
    within(bracketRegion).getByText(/third advanced without an opponent|third raqibsiz/i),
  ).toBeVisible();

  // The final is **waiting**, not a bye.
  const final = nodes[2] as HTMLElement;
  expect(within(final).getByText(/waiting for an opponent|raqib kutilmoqda/i)).toBeVisible();
  expect(within(final).queryByText(/^bye$|raqibsiz o'tish/i)).toBeNull();
  // An empty future seat is named, never blank and never a fake player.
  expect(within(final).getByText(/to be decided|hali aniqlanmagan/i)).toBeVisible();
  // A pending node offers no link at all.
  expect(within(final).queryByRole("link")).toBeNull();

  // The live node links to the **real** game route with the real match id.
  const live = nodes[0] as HTMLElement;
  expect(within(live).getByRole("link", { name: /watch/i })).toHaveAttribute(
    "href",
    `/games/${LIVE_MATCH}`,
  );
  expect(within(live).queryByRole("link", { name: /replay/i })).toBeNull();
});

it("shows a finished tournament's standings with tied ranks intact and a real replay link", async () => {
  // §29.7 and §29.10. Ranks are **not dense**: two players knocked out in
  // the same round share third, and there is no fourth place. Renumbering
  // would publish a comparison the bracket never made.
  serveDetail(
    tournament({ id: DONE_ID, status: "completed", completed_at: "2026-08-04T20:00:00Z" }),
    {
      bracketBody: completedBracket(),
      standingsBody: standings(),
    },
  );
  renderApp({ path: `/tournaments/${DONE_ID}` });

  const table = await screen.findByRole("table", undefined, { timeout: 3000 });
  const rows = within(table).getAllByRole("row").slice(1);
  // The **leading** number: a shared rank also carries a screen-reader-only
  // "Tied for 3", so stripping every non-digit would read "33".
  const ranks = rows.map(
    (row) => /^\d+/.exec(within(row).getAllByRole("cell")[0]?.textContent?.trim() ?? "")?.[0],
  );
  expect(ranks).toEqual(["1", "3", "3"]);
  // Non-dense: there is no fourth place, and nothing renumbered one in.
  expect(ranks).not.toContain("4");

  // The table has real column headers, so a screen reader can associate them.
  expect(within(table).getAllByRole("columnheader").length).toBeGreaterThanOrEqual(8);

  // A finished tournament offers no way to enter it.
  expect(
    screen.queryByRole("button", { name: /enter tournament|turnirga yozilish/i }),
  ).toBeNull();

  // And its bracket links to the replay, not to the live game.
  const bracketRegion = screen.getByRole("region", {
    name: /bracket rounds|setka bosqichlari/i,
  });
  expect(within(bracketRegion).getByRole("link", { name: /replay/i })).toHaveAttribute(
    "href",
    `/games/${PLAYED_MATCH}/replay`,
  );
  expect(within(bracketRegion).queryByRole("link", { name: /watch/i })).toBeNull();
});

it("polls a moving tournament and leaves a finished one alone", async () => {
  // §29.8. Both halves in one test, because the assertion that matters is
  // the *difference*: a suite that only proved polling happens would pass
  // with an interval that never stops, which is the failure mode — a
  // completed tournament re-read every eight seconds for as long as the tab
  // is open, forever, for an answer that cannot change.
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    serveDetail(tournament({ status: "in_progress", current_round: 1 }));
    const moving = renderApp({ path: `/tournaments/${OPEN_ID}` });
    await screen.findByRole(
      "region",
      { name: /bracket rounds|setka bosqichlari/i },
      { timeout: 3000 },
    );

    const before = requests.filter((entry) => entry === "bracket").length;
    await vi.advanceTimersByTimeAsync(25_000);
    const after = requests.filter((entry) => entry === "bracket").length;
    // 25s at an 8s interval is three ticks — bounded, and not a tight loop.
    expect(after).toBeGreaterThan(before);
    expect(after - before).toBeLessThanOrEqual(4);
    moving.unmount();

    requests = [];
    serveDetail(
      tournament({ id: DONE_ID, status: "completed", completed_at: "2026-08-04T20:00:00Z" }),
      { bracketBody: completedBracket(), standingsBody: standings() },
    );
    renderApp({ path: `/tournaments/${DONE_ID}` });
    await screen.findByRole("table", undefined, { timeout: 3000 });

    const settled = requests.filter((entry) => entry === "bracket").length;
    await vi.advanceTimersByTimeAsync(30_000);
    expect(requests.filter((entry) => entry === "bracket")).toHaveLength(settled);
  } finally {
    vi.useRealTimers();
  }
});

/**
 * The entry CTA's full label in each locale.
 *
 * Not `/sign in/i`: the public header offers "Sign in" as well, and a
 * pattern that matches both would find two links and fail on the ambiguity
 * rather than on the behaviour.
 */
const SIGN_IN_TO_ENTER = /sign in to enter|yozilish uchun kiring|войдите, чтобы записаться/i;

/** No session: the refresh that bootstraps one is refused. */
function serveAnonymous() {
  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json({ code: "invalid_token", message: "No." }, { status: 401 }),
    ),
  );
}

it("shows the lobby to a visitor with no account", async () => {
  // A64-026.4 §43.5, and the **replacement** for a test that asserted the
  // opposite. That test was right while every tournament handler took
  // `CurrentUser`: an unguarded route would have rendered a page whose
  // requests all took a `401`. The handlers now answer without one, so the
  // requirement it encoded is the outdated half — not the code.
  serveAnonymous();
  serveLobby([tournament()]);

  const { unmount } = renderApp({ path: "/tournaments" });

  expect(await screen.findByText("Sunday Open")).toBeVisible();
  unmount();
});

it("asks a visitor with no account to sign in rather than offering to enter", async () => {
  // The line the open route does not cross. Reading is open; entering is
  // not, and the panel has to say so — a disabled button whose reason is
  // invisible is the failure §22 named.
  serveAnonymous();
  serveDetail(tournament());

  const { unmount } = renderApp({ path: `/tournaments/${OPEN_ID}` });

  const cta = await screen.findByRole(
    "link",
    { name: SIGN_IN_TO_ENTER },
    {
      timeout: 3000,
    },
  );
  expect(cta).toBeVisible();
  // `next` carries them back to the tournament they were reading.
  expect(cta).toHaveAttribute("href", expect.stringContaining(OPEN_ID));
  expect(
    screen.queryByRole("button", { name: /yozilish|enter tournament|записаться/i }),
  ).toBeNull();

  unmount();
});

it("offers no game link to a visitor who cannot open a game", async () => {
  // The bracket's "Watch" and "Replay" go to `/games/…`, which stayed
  // behind `protectedPage` when the tournament routes came out from behind
  // it. Offering them to a visitor with no account would be a link that
  // lies about where it goes — three of them per finished node.
  serveAnonymous();
  serveDetail(tournament({ status: "in_progress", current_round: 1 }));

  const { unmount } = renderApp({ path: `/tournaments/${OPEN_ID}` });
  await screen.findByRole("link", { name: SIGN_IN_TO_ENTER }, { timeout: 3000 });

  // The bracket is there — otherwise this test would pass on an empty page.
  expect(screen.getByText("Rival")).toBeVisible();

  const links = screen.getAllByRole("link");
  expect(links.filter((link) => link.getAttribute("href")?.includes("/games/"))).toHaveLength(
    0,
  );

  unmount();
});

it("never asks the server whether an anonymous visitor is registered", async () => {
  // `/registrations/me` keeps its token. Firing it anyway would spend a
  // guaranteed `401` on every public tournament page — and the response
  // would land in the panel as a failure rather than as "not signed in".
  serveAnonymous();
  serveDetail(tournament());

  const { unmount } = renderApp({ path: `/tournaments/${OPEN_ID}` });
  await screen.findByRole("link", { name: SIGN_IN_TO_ENTER }, { timeout: 3000 });

  expect(requests).not.toContain("registration");
  unmount();
});
