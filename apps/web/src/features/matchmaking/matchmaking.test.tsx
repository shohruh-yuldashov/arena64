import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PendingMatch, QueueTicket } from "@/entities/queue";
import type { TimeControl } from "@/entities/time-control";
import { derive } from "@/features/matchmaking/model/lobby-state";
import { httpClient } from "@/shared/api/client";
import { env } from "@/shared/config/env";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * The lobby, through the real app — A64-020.5A §27, §28.
 *
 * Every test that touches a screen mounts `App`: the real router, the real
 * `RequireAuth`, the provider graph and the live Axios instance. A page
 * rendered directly would prove a component works and say nothing about
 * whether `/play` exists, whether the guard is on it, or whether the
 * request carries a token — which is exactly the "implemented and reachable
 * from nothing" failure this codebase has found repeatedly.
 *
 * The one exception is the precedence test, which calls `derive` directly.
 * That rule is a pure function of two server reads and is the single most
 * important thing in this feature; asserting it through the DOM would test
 * it *and* the rendering, and would still not cover the combinations.
 */
/**
 * The suite runs in **English**: `I18nProvider` falls back to `en` with no
 * stored preference, which is what a fresh browser gets. Asserting against
 * Uzbek would mean setting a locale in every test to check strings that are
 * not what this feature is about — and the accessible *names* being right
 * is the property, in whatever language the reader has.
 */
const url = (path: string) => `${env.VITE_API_URL}${path}`;
const REFRESH = url("/auth/browser/refresh");
const CONTROLS = url("/time-controls");
const QUEUE = url("/matchmaking/queue");
const MY_QUEUE = url("/matchmaking/queue/me");
const PENDING = url("/matchmaking/matches/pending");

const VIEWER = {
  id: "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
  username: "viewer",
  display_name: "Viewer",
  email: "viewer@example.com",
  is_active: true,
  is_verified: true,
};

function envelope<T>(data: T) {
  return { data, meta: { request_id: null, correlation_id: null } };
}

function problem(status: number, code: string) {
  return HttpResponse.json(
    { code, message: "Nope.", request_id: null, correlation_id: null },
    { status },
  );
}

/**
 * The catalogue exactly as the seeded backend returns it.
 *
 * Typed against the **generated** response, not left as a loose literal:
 * a fixture that has drifted from the contract is a suite that passes over
 * a shape the server cannot produce, which is the failure mode MSW exists
 * to avoid in the first place.
 */
const CATALOGUE: TimeControl[] = [
  {
    id: "bullet_1_0",
    label: "1+0",
    base_time_ms: 60_000,
    increment_ms: 0,
    speed_class: "bullet",
  },
  {
    id: "blitz_3_2",
    label: "3+2",
    base_time_ms: 180_000,
    increment_ms: 2_000,
    speed_class: "blitz",
  },
  {
    id: "rapid_10_0",
    label: "10+0",
    base_time_ms: 600_000,
    increment_ms: 0,
    speed_class: "rapid",
  },
  {
    id: "classical_30_0",
    label: "30+0",
    base_time_ms: 1_800_000,
    increment_ms: 0,
    speed_class: "classical",
  },
];

const TICKET: QueueTicket = {
  ticket_id: "019fd0aa-1111-7000-8000-000000000001",
  variant: "russian_8x8",
  queue_type: "casual",
  region: "global",
  status: "waiting",
  time_control_id: "blitz_3_2",
  base_time_ms: 180_000,
  increment_ms: 2_000,
  speed_class: "blitz",
  rating_snapshot: 1500,
  entered_at: "2026-08-05T10:00:00Z",
  expires_at: "2026-08-05T10:10:00Z",
  waiting: 2,
};

function offer(overrides: Partial<PendingMatch> = {}): PendingMatch {
  return {
    match_id: "019fd0bb-2222-7000-8000-000000000002",
    status: "pending_acceptance",
    your_side: "light",
    opponent: {
      player_id: "019fd0cc-3333-7000-8000-3",
      username: "rival",
      display_name: "Rival",
    },
    variant: "russian_8x8",
    rated: false,
    base_time_ms: 180_000,
    increment_ms: 2_000,
    speed_class: "blitz",
    acceptance_deadline: "2026-08-05T10:00:30Z",
    you_accepted: false,
    opponent_accepted: false,
    created_at: "2026-08-05T10:00:00Z",
    ...overrides,
  };
}

function signedIn() {
  mswServer.use(
    http.post(REFRESH, () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: VIEWER })),
    ),
  );
}

/** An idle lobby: catalogue present, nothing queued, nothing offered. */
function idleLobby() {
  mswServer.use(
    http.get(CONTROLS, () => HttpResponse.json(envelope(CATALOGUE))),
    http.get(MY_QUEUE, () => problem(404, "not_found")),
    http.get(PENDING, () => problem(404, "not_found")),
  );
}

beforeEach(() => {
  // The same reset `profile.test.tsx` performs, and for the same reason:
  // `SessionProvider` installs an auth interceptor on mount and each test
  // mounts a fresh app, so without this the single-flight refresh runs
  // against a stack of interceptors from every previous test in the file —
  // which surfaces as a render loop in the router rather than as anything
  // resembling its cause.
  vi.spyOn(console, "error").mockImplementation(() => {});
  httpClient.interceptors.request.clear();
  httpClient.interceptors.response.clear();
  vi.useRealTimers();
  signedIn();
});

describe("the route", () => {
  it("turns an anonymous visitor away rather than rendering the lobby", async () => {
    // §28. The guard is asserted through the *real* router, because a
    // `RequireAuth` that was never put on `/play` is a page that calls
    // `/matchmaking/queue/me` unauthenticated and looks like a load error.
    mswServer.use(http.post(REFRESH, () => problem(401, "invalid_session")));

    const { unmount } = renderApp({ path: "/play" });

    expect(await screen.findByRole("heading", { name: /kirish|sign in|вход/i })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Start a game" })).not.toBeInTheDocument();

    // Explicit, like `profile.test.tsx`'s guard test. The router begins
    // loading `/play`'s lazy chunk before the redirect lands, and letting
    // that resolve into a torn-down root is an unhandled rejection with
    // nothing to do with the assertion above.
    unmount();
  });
});

describe("choosing a game", () => {
  it("renders the server's catalogue and submits the identifier it came with", async () => {
    // §4 and §27.2. The two halves of one property: nothing is hardcoded
    // on the way in, and nothing is re-derived on the way out. A client
    // that built `base_time_ms` from the id would pass the first half of
    // this and fail the second.
    idleLobby();
    const sent: unknown[] = [];
    mswServer.use(
      http.post(QUEUE, async ({ request }) => {
        sent.push(await request.json());
        return HttpResponse.json(envelope(TICKET), { status: 201 });
      }),
    );

    renderApp({ path: "/play" });

    const controls = await screen.findByRole("group", { name: /time control/i });

    // §28's reachability claim, asserted where a signed-in shell is already
    // on screen: the lobby has an entry point. A route that exists and no
    // header links to is the "implemented and reachable from nothing"
    // failure, and it is invisible to every other test in this file because
    // they all navigate by URL.
    expect(screen.getByRole("link", { name: "Play" })).toHaveAttribute("href", "/play");

    // Rendered from the response, formatted by `Intl` — four options, and
    // the labels are the durations rather than the identifiers.
    expect(within(controls).getAllByRole("radio")).toHaveLength(4);
    await userEvent.click(within(controls).getByRole("radio", { name: /3\+2/ }));
    await userEvent.click(screen.getByRole("button", { name: /join the queue/i }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toMatchObject({ time_control_id: "blitz_3_2", queue_type: "casual" });
    // No clock numbers on the wire. The catalogue is authoritative and the
    // endpoint would refuse them anyway; asserting it here is what stops a
    // well-meaning refactor from "helpfully" including them.
    expect(sent[0]).not.toHaveProperty("base_time_ms");
    expect(sent[0]).not.toHaveProperty("increment_ms");
  });

  it("joins once, however many times the button is pressed", async () => {
    // §6 and §27.4. The button is disabled while the request is in flight,
    // so a double-click is one ticket — QT-1 would refuse the second with a
    // `409`, and a lobby that produced one on every impatient click would
    // show a conflict error for something it did itself.
    let joins = 0;
    mswServer.use(
      http.get(CONTROLS, () => HttpResponse.json(envelope(CATALOGUE))),
      http.get(PENDING, () => problem(404, "not_found")),
      // The ticket exists only once the join has been accepted, which is
      // what makes the lobby show the form to begin with.
      http.get(MY_QUEUE, () =>
        joins === 0 ? problem(404, "not_found") : HttpResponse.json(envelope(TICKET)),
      ),
      http.post(QUEUE, async () => {
        joins += 1;
        await new Promise((resolve) => setTimeout(resolve, 50));
        return HttpResponse.json(envelope(TICKET), { status: 201 });
      }),
    );

    renderApp({ path: "/play" });

    const controls = await screen.findByRole("group", { name: /time control/i });
    await userEvent.click(within(controls).getByRole("radio", { name: /3\+2/ }));
    const submit = screen.getByRole("button", { name: /join the queue/i });
    await userEvent.click(submit);
    await userEvent.click(submit);
    await userEvent.click(submit);

    expect(await screen.findByText(/searching for an opponent/i)).toBeVisible();
    expect(joins).toBe(1);
  });
});

describe("recovery", () => {
  it("shows the offer, not the queue, when the server reports both", () => {
    // §8, §9 and §27.3 — **the** rule of this feature.
    //
    // Pairing consumes a ticket and creates a match in two transactions, so
    // a client polling across the gap legitimately sees a live ticket
    // beside a real offer, or a `404` beside one. Every combination must
    // land on the offer: it is the state with a deadline, and losing it
    // costs the player a game.
    const match = offer();

    expect(derive({ session: true, ticket: TICKET, match }).status).toBe("match_offer");
    expect(derive({ session: true, ticket: null, match }).status).toBe("match_offer");
    // A ticket alone is a waiting player; neither is an idle lobby.
    expect(derive({ session: true, ticket: TICKET, match: null }).status).toBe("queued");
    expect(derive({ session: true, ticket: null, match: null }).status).toBe("idle");
    // An offer this player already answered keeps the dialog up rather
    // than falling back to the queue behind it.
    expect(
      derive({ session: true, ticket: null, match: offer({ you_accepted: true }) }).status,
    ).toBe("awaiting_opponent");
    // A settled offer is not an offer. `active` has somewhere to go.
    expect(
      derive({ session: true, ticket: null, match: offer({ status: "active" }) }).status,
    ).toBe("transitioning");
    expect(
      derive({ session: true, ticket: TICKET, match: offer({ status: "cancelled" }) }).status,
    ).toBe("queued");
    // Nothing is concluded before the session resolves.
    expect(derive({ session: false, ticket: null, match: null }).status).toBe("bootstrapping");
  });

  it("reconstructs the waiting state from the server after a reload", async () => {
    // §9. There is no restore branch in the page — a reload runs the same
    // two queries a first visit does — so what this asserts is that the
    // *chosen* pool comes back, not merely that something rendered.
    mswServer.use(
      http.get(CONTROLS, () => HttpResponse.json(envelope(CATALOGUE))),
      http.get(MY_QUEUE, () => HttpResponse.json(envelope(TICKET))),
      http.get(PENDING, () => problem(404, "not_found")),
    );

    renderApp({ path: "/play" });

    expect(await screen.findByText(/searching for an opponent/i)).toBeVisible();
    expect(screen.getByText("3+2")).toBeVisible();
    expect(screen.getByText("Blitz")).toBeVisible();
    expect(screen.getByText("Casual")).toBeVisible();
  });
});

describe("cancelling", () => {
  it("shows the offer when a pairing won the race", async () => {
    // §13 and §27.5. `DELETE` answers `204` whether or not there was a
    // ticket, so a `204` does **not** mean "you are idle" — the ticket may
    // have been consumed a moment earlier. The mutation re-reads both, and
    // the offer must win: telling the player "cancelled" while a live match
    // waits behind the page is how a game is lost to a race.
    let paired = false;
    mswServer.use(
      http.get(CONTROLS, () => HttpResponse.json(envelope(CATALOGUE))),
      http.get(MY_QUEUE, () =>
        paired ? problem(404, "not_found") : HttpResponse.json(envelope(TICKET)),
      ),
      http.get(PENDING, () =>
        paired ? HttpResponse.json(envelope(offer())) : problem(404, "not_found"),
      ),
      http.delete(QUEUE, () => {
        paired = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    renderApp({ path: "/play" });

    await userEvent.click(await screen.findByRole("button", { name: /^cancel$/i }));

    expect(await screen.findByRole("alertdialog")).toBeVisible();
    expect(screen.getByText("Rival")).toBeVisible();
  });
});

describe("the offer", () => {
  it("interrupts, names both answers and does not announce every second", async () => {
    // §14, §15, §23 and §27.6. Four accessibility properties in one place
    // because they describe one component's contract:
    //
    //   - `alertdialog`, so assistive technology treats it as interrupting
    //   - both answers are buttons with the opponent in their name
    //   - the countdown is `aria-live="off"` — a per-second live region is
    //     a screen reader saying a number thirty times
    //   - `Escape` does nothing, because dismissing would leave a match
    //     the player never answered quietly expiring
    mswServer.use(
      http.get(CONTROLS, () => HttpResponse.json(envelope(CATALOGUE))),
      http.get(MY_QUEUE, () => problem(404, "not_found")),
      http.get(PENDING, () => HttpResponse.json(envelope(offer()))),
    );

    renderApp({ path: "/play" });

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByRole("button", { name: /accept — rival/i })).toBeVisible();
    expect(within(dialog).getByRole("button", { name: /decline — rival/i })).toBeVisible();
    expect(within(dialog).getByText("3+2")).toBeVisible();

    const counter = dialog.querySelector('[aria-live="off"]');
    expect(counter).not.toBeNull();

    await userEvent.keyboard("{Escape}");
    expect(await screen.findByRole("alertdialog")).toBeVisible();
  });

  it("waits for the opponent, then hands off once both have accepted", async () => {
    // §16 and §27.7. Both halves, because the first without the second is
    // a dialog that never resolves and the second without the first is a
    // navigation that fires while the opponent is still deciding.
    let bothAccepted = false;
    mswServer.use(
      http.get(CONTROLS, () => HttpResponse.json(envelope(CATALOGUE))),
      http.get(MY_QUEUE, () => problem(404, "not_found")),
      http.get(PENDING, () =>
        HttpResponse.json(
          envelope(
            bothAccepted
              ? offer({ status: "active", you_accepted: true, opponent_accepted: true })
              : offer({ you_accepted: true }),
          ),
        ),
      ),
      http.post(`${QUEUE.replace("/queue", "")}/matches/:id/accept`, () =>
        HttpResponse.json(
          envelope(
            bothAccepted
              ? offer({ status: "active", you_accepted: true, opponent_accepted: true })
              : offer({ you_accepted: true }),
          ),
        ),
      ),
    );

    renderApp({ path: "/play" });

    // The opponent has not answered: the dialog stays, and says so.
    expect(await screen.findByText(/waiting for your opponent/i)).toBeVisible();
    expect(screen.getByRole("alertdialog")).toBeVisible();

    // The opponent answers. The next poll carries `active` and the lobby
    // navigates — asserted by the handoff page, not by a router spy, so
    // what is proven is that the route exists and renders.
    bothAccepted = true;
    expect(
      await screen.findByRole("heading", { name: "Game" }, { timeout: 5000 }),
    ).toBeVisible();

    // A64-020.5B replaced the handoff placeholder with the live board, so
    // the destination is no longer identified by a rendered match id — the
    // board renders a position, not an identifier. What is asserted instead
    // is that the *game* page mounted and the lobby is gone.
    //
    // "the two players reached the **same** match" is not lost: it is
    // asserted where it can actually be observed, by comparing the two
    // browsers' URLs in `tests/e2e/play.spec.ts`.
    expect(await screen.findByLabelText(/loading the game/i)).toBeVisible();
    expect(screen.queryByRole("group", { name: /time control/i })).not.toBeInTheDocument();
  });
});
