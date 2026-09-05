import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { App } from "@/app/App";
import { createAdminRouter } from "@/app/router";
import { accessToken } from "@/app/session-store";

/**
 * Product analytics — A64-027.6 §47–§54.
 *
 * The tests worth writing here are the ones an operator could not detect by
 * looking at the screen. A dashboard that shows a plausible number is
 * indistinguishable from one that shows a correct number, so each of these
 * asserts something about *honesty* rather than about layout:
 *
 *   - an unmeasurable rate is a dash, and `0` is still `0%`
 *   - a period that has not finished says so
 *   - a failed refresh keeps the last known figures instead of zeroing them
 *   - the page never recomputes a metric the backend already defines
 *   - nothing polls
 */

const ADMIN = { id: "a", username: "op", display_name: "Op", roles: ["admin"] };
const VIEWER = { id: "v", username: "viewer", display_name: "V", roles: ["moderator"] };

const meta = (overrides: Record<string, unknown> = {}) => ({
  environment: "production",
  include_synthetic: false,
  period_start: "2026-08-05",
  period_end: "2026-09-03",
  requested_start: "2026-08-05",
  requested_end: "2026-09-03",
  maturity: "mature",
  coverage: "complete",
  generated_at: "2026-09-04T09:00:00Z",
  ...overrides,
});

const overview = (overrides: Record<string, unknown> = {}) => ({
  active_players: {
    as_of: "2026-09-03",
    daily: 128,
    weekly: 640,
    monthly: 2048,
    stickiness: 0.0625,
  },
  activation: {
    stages: [
      {
        stage: "user_registered",
        subjects: 400,
        conversion_from_previous: null,
        conversion_from_start: null,
        drop_off: 0,
      },
      {
        stage: "email_verified",
        subjects: 300,
        conversion_from_previous: 0.75,
        conversion_from_start: 0.75,
        drop_off: 100,
      },
      {
        stage: "activated",
        subjects: 120,
        conversion_from_previous: 0.4,
        conversion_from_start: 0.3,
        drop_off: 180,
      },
    ],
    overall_conversion: 0.3,
    time_to_activation: { sample: 120, median_seconds: 94, p95_seconds: 3600 },
    time_to_verify: { sample: 300, median_seconds: 42, p95_seconds: 600 },
    meta: meta(),
  },
  matchmaking: {
    grain: "queue_attempt",
    queue_joins: 900,
    paired_attempts: 800,
    abandoned_attempts: 100,
    cancelled_attempts: 0,
    expired_attempts: 0,
    abandonment_rate: 0.111,
    match_found_rate: 0.888,
    wait: { sample: 800, p50_seconds: 12.5, p95_seconds: 61 },
    offers_accepted: 700,
    offers_declined: 60,
    offers_expired: 40,
    offers_resolved: 800,
    offer_acceptance: 0.875,
    meta: meta(),
  },
  games: {
    grain: "match",
    started: 700,
    completed: 650,
    aborted: 20,
    completion_rate: 0.956,
    resignation_rate: 0.2,
    draw_rate: 0,
    abandonment_rate: 0.05,
    rated_share: 0.9,
    resignations: 130,
    draws: 0,
    abandonments: 33,
    flags: 40,
    rated_completions: 585,
    termination_breakdown: [
      { reason: "no_legal_moves", matches: 400 },
      { reason: "resignation", matches: 130 },
      { reason: "flag", matches: 40 },
    ],
    meta: meta(),
  },
  engagement: {
    week_start: "2026-08-24",
    week_end: "2026-08-30",
    active_players: 640,
    match_starts: 1600,
    matches_per_active_player: 2.5,
    median_matches_per_active_player: 2,
    tournament_entrants: 64,
    tournament_participation: 0.1,
    friendships_created: 25,
    challenges_sent: 200,
    challenges_accepted: 150,
    challenges_declined: 30,
    challenges_expired: 20,
    challenges_cancelled: 0,
    challenge_acceptance: 0.75,
    meta: meta(),
  },
  meta: meta(),
  ...overrides,
});

const retention = (overrides: Record<string, unknown> = {}) => ({
  rows: [
    {
      cohort_day: "2026-08-01",
      cohort: 50,
      d1: 20,
      d7: 10,
      d30: 5,
      d1_rate: 0.4,
      d7_rate: 0.2,
      d30_rate: 0.1,
    },
    {
      // Registered yesterday: D1 has not happened yet, D7 and D30 cannot
      // have. Three dashes, and not one zero.
      cohort_day: "2026-09-03",
      cohort: 40,
      d1: null,
      d7: null,
      d30: null,
      d1_rate: null,
      d7_rate: null,
      d30_rate: null,
    },
  ],
  meta: meta(),
  ...overrides,
});

const acquisition = (overrides: Record<string, unknown> = {}) => ({
  stages: [
    {
      stage: "landing_viewed",
      subjects: 10,
      conversion_from_previous: null,
      conversion_from_start: null,
      drop_off: 0,
    },
    {
      stage: "register_cta_clicked",
      subjects: 4,
      conversion_from_previous: 0.4,
      conversion_from_start: 0.4,
      drop_off: 6,
    },
  ],
  overall_conversion: 0.1,
  registrations_in_range: 400,
  meta: meta(),
  ...overrides,
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

interface Stub {
  urls: string[];
  overviewStatuses: number[];
  overviewCalls: number;
}

function stubApi(
  bodies: {
    overview?: () => unknown;
    retention?: () => unknown;
    acquisition?: () => unknown;
    me?: unknown;
  } = {},
  overviewStatuses: number[] = [],
): Stub {
  const stub: Stub = { urls: [], overviewStatuses, overviewCalls: 0 };

  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    if (url.endsWith("/admin/me")) {
      return Promise.resolve(json({ data: bodies.me ?? ADMIN }));
    }
    stub.urls.push(url);
    if (url.includes("/admin/analytics/overview")) {
      const status = overviewStatuses[stub.overviewCalls] ?? 200;
      stub.overviewCalls += 1;
      return Promise.resolve(json({ data: (bodies.overview ?? overview)() }, status));
    }
    if (url.includes("/admin/analytics/retention")) {
      return Promise.resolve(json({ data: (bodies.retention ?? retention)() }));
    }
    if (url.includes("/admin/analytics/acquisition")) {
      return Promise.resolve(json({ data: (bodies.acquisition ?? acquisition)() }));
    }
    return Promise.resolve(json({}, 404));
  });

  return stub;
}

function renderAnalytics() {
  const router = createAdminRouter(createMemoryHistory({ initialEntries: ["/analytics"] }));
  render(<App router={router} />);
  return router;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  accessToken.clear();
});

/** The row an operator would misread as "nobody came back". */
function retentionRow(cohortDay: string): HTMLElement {
  const header = screen.getByRole("rowheader", { name: cohortDay });
  const row = header.closest("tr");
  if (row === null) throw new Error(`no row for ${cohortDay}`);
  return row;
}

it("renders an unmeasurable retention rate as a dash and never as zero", async () => {
  stubApi();
  renderAnalytics();

  await screen.findByRole("heading", { name: "Product analytics" });
  await waitFor(() => {
    expect(screen.getByRole("rowheader", { name: "2026-09-03" })).toBeInTheDocument();
  });

  const immature = retentionRow("2026-09-03");
  const cells = within(immature).getAllByRole("cell");
  // [size, D1, D7, D30]
  expect(cells[1]).toHaveAttribute("data-unmeasured", "true");
  expect(cells[2]).toHaveAttribute("data-unmeasured", "true");
  expect(cells[3]).toHaveAttribute("data-unmeasured", "true");
  expect(immature.textContent).not.toContain("0%");

  // The mature cohort in the same table proves the dash is about the data,
  // not about the renderer.
  const mature = retentionRow("2026-08-01");
  expect(within(mature).getByText("40%")).toBeInTheDocument();
});

it("renders a genuine zero rate as 0%, not as a dash", async () => {
  stubApi();
  renderAnalytics();

  // `draw_rate` is 0 in the fixture: zero draws is a fact, not an absence.
  const drawRate = await screen.findByText("Draws");
  const value = drawRate.closest(".metric")?.querySelector(".metric__value");
  expect(value).toHaveTextContent("0%");
  expect(value).not.toHaveAttribute("data-unmeasured");
});

it("marks a period that has not finished, and one narrower than requested", async () => {
  stubApi({
    overview: () => overview({ meta: meta({ maturity: "partial" }) }),
    retention: () =>
      retention({
        meta: meta({
          coverage: "truncated",
          requested_start: "2026-06-01",
          period_start: "2026-08-05",
        }),
      }),
  });
  renderAnalytics();

  expect(await screen.findByText("Partial period")).toBeInTheDocument();
  const truncated = await screen.findByText("Coverage limited");
  // The badge must say which period was actually answered — "limited" on
  // its own leaves the 90-day label standing beside a 30-day answer.
  expect(truncated).toHaveTextContent("2026-08-05");
  expect(truncated).toHaveTextContent("2026-09-03");
});

it("keeps the last known figures when a refresh fails", async () => {
  stubApi({}, [200, 500]);
  renderAnalytics();

  const dau = await screen.findByText("DAU");
  expect(dau.closest(".metric")).toHaveTextContent("128");

  await userEvent.click(screen.getByRole("button", { name: "Refresh" }));

  await screen.findByText("Analytics could not be loaded.");
  // The numbers are still the ones that were true, not zeros.
  expect(screen.getByText("DAU").closest(".metric")).toHaveTextContent("128");
});

it("shows an error with a retry when the first load fails", async () => {
  stubApi({}, [500, 200]);
  renderAnalytics();

  await screen.findByText("Analytics could not be loaded.");
  expect(screen.queryByText("DAU")).not.toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(await screen.findByText("DAU")).toBeInTheDocument();
});

it("asks the server for a new window when the range changes, and never polls", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const stub = stubApi();
  renderAnalytics();

  await screen.findByText("DAU");
  const firstOverview = stub.urls.filter((url) => url.includes("/overview"));
  expect(firstOverview).toHaveLength(1);
  expect(firstOverview[0]).toMatch(/start=\d{4}-\d{2}-\d{2}&end=\d{4}-\d{2}-\d{2}/);

  await userEvent.click(screen.getByRole("button", { name: "7 days" }));
  await waitFor(() => {
    expect(stub.urls.filter((url) => url.includes("/overview"))).toHaveLength(2);
  });

  const before = stub.urls.length;
  await vi.advanceTimersByTimeAsync(120_000);
  expect(stub.urls).toHaveLength(before);
});

it("requests only the three aggregate reads, and no raw event endpoint", async () => {
  const stub = stubApi();
  renderAnalytics();

  await screen.findByText("DAU");
  await waitFor(() => {
    expect(stub.urls).toHaveLength(3);
  });

  const sections = stub.urls.map((url) => /analytics\/(\w+)/.exec(url)?.[1]);
  expect(new Set(sections)).toEqual(new Set(["overview", "retention", "acquisition"]));
  expect(stub.urls.some((url) => url.includes("/events"))).toBe(false);
});

it("shows the funnel's own conversions rather than recomputing them", async () => {
  stubApi({
    overview: () =>
      overview({
        activation: {
          ...overview().activation,
          stages: [
            {
              stage: "user_registered",
              subjects: 400,
              conversion_from_previous: null,
              conversion_from_start: null,
              drop_off: 0,
            },
            {
              // Deliberately *not* 300/400. If the page divides, it prints
              // 75%; the server said 60% and the server owns the formula.
              stage: "email_verified",
              subjects: 300,
              conversion_from_previous: 0.6,
              conversion_from_start: 0.55,
              drop_off: 100,
            },
          ],
        },
      }),
  });
  renderAnalytics();

  const stage = await screen.findByText("Email verified");
  const row = stage.closest("tr");
  expect(row).not.toBeNull();
  expect(within(row as HTMLElement).getByText("60%")).toBeInTheDocument();
  expect(within(row as HTMLElement).getByText("55%")).toBeInTheDocument();
  expect(within(row as HTMLElement).queryByText("75%")).not.toBeInTheDocument();
});

it("states the acquisition funnel's measurement gap beside it", async () => {
  stubApi();
  renderAnalytics();

  await screen.findByRole("heading", { name: "Acquisition" });
  expect(
    screen.getByText(/Anonymous-to-account stitching coverage is near zero/),
  ).toBeInTheDocument();
  // The registrations the period truly saw, against the funnel's ten views.
  expect(screen.getByText(/Registrations in this period: 400/)).toBeInTheDocument();
});

it("reports no completion rate per speed class", async () => {
  stubApi();
  renderAnalytics();

  await screen.findByText("Completion rate");
  for (const speed of ["bullet", "blitz", "rapid", "classical", "Bullet", "Blitz"]) {
    expect(screen.queryByText(new RegExp(speed))).not.toBeInTheDocument();
  }
});

it("labels the matchmaking grain so no figure claims to be a count of players", async () => {
  stubApi();
  renderAnalytics();

  const joins = await screen.findByText("Queue joins");
  expect(joins.closest(".metric")).toHaveTextContent(
    "Grain: queue attempt — one player may join several times.",
  );
});

it("says the period is empty rather than drawing an empty funnel", async () => {
  stubApi({
    overview: () =>
      overview({
        activation: { ...overview().activation, stages: [], overall_conversion: null },
        games: { ...overview().games, termination_breakdown: [] },
      }),
    retention: () => retention({ rows: [] }),
    acquisition: () =>
      acquisition({ stages: [], overall_conversion: null, registrations_in_range: 0 }),
  });
  renderAnalytics();

  await waitFor(() => {
    expect(screen.getAllByText("No data for this period").length).toBeGreaterThanOrEqual(3);
  });
  // An absent funnel is a sentence, not a table of zeroes.
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  expect(screen.getByText("Activation rate").closest(".metric")).toHaveTextContent("—");
});

it("renders no untranslated keys", async () => {
  stubApi();
  const { container } = render(<div />);
  container.remove();
  renderAnalytics();

  await screen.findByText("DAU");
  expect(document.body.textContent ?? "").not.toMatch(/analytics\.[a-zA-Z]/);
});

/**
 * The role gate is the server's, not the console's — `ProtectedLayout`
 * believes `/admin/me`, and `/admin/analytics` is guarded by `CurrentAdmin`
 * (proved in `tests/contract/test_admin_analytics_api.py`). What matters
 * here is that a refusal never becomes a screen full of zeroes.
 */
it("shows a denial rather than the console when the server refuses the session", async () => {
  stubApi({ me: VIEWER });
  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    return Promise.resolve(json({ error: { code: "forbidden" } }, 403));
  });
  renderAnalytics();

  expect(await screen.findByRole("alert")).toBeInTheDocument();
  expect(screen.queryByText("DAU")).not.toBeInTheDocument();
});

it("does not turn a refused analytics read into zeroes", async () => {
  stubApi({}, [403]);
  renderAnalytics();

  await screen.findByText("Analytics could not be loaded.");
  expect(screen.queryByText("DAU")).not.toBeInTheDocument();
  expect(screen.queryByText("0%")).not.toBeInTheDocument();
});

/**
 * Recorded from the real endpoint over real PostgreSQL, by seeding five
 * started matches (four completed: two resignations, one agreed draw, one
 * flag) and capturing `GET /admin/analytics/overview` verbatim. Checked in
 * so a change to the wire contract breaks a test here rather than a number
 * on a screen — the fixtures above are hand-written and would keep passing
 * against a field the API no longer sends.
 */
it("renders the values a real seeded backend returned, unchanged", async () => {
  const captured = (await import("./__fixtures__/analytics-overview.captured.json")).default;
  stubApi({ overview: () => captured });
  renderAnalytics();

  await screen.findByText("Matches started");
  const valueOf = (label: string) =>
    screen.getByText(label).closest(".metric")?.querySelector(".metric__value")?.textContent;

  // started 5 · completed 4 · completion_rate 0.8 · resignation_rate 0.5
  // · draw_rate 0.25 · rated_share 1.0, straight from the capture.
  expect(valueOf("Matches started")).toBe("5");
  expect(valueOf("Completed")).toBe("4");
  expect(valueOf("Completion rate")).toBe("80%");
  expect(valueOf("Resignations")).toBe("50%");
  expect(valueOf("Draws")).toBe("25%");
  expect(valueOf("Rated share")).toBe("100%");

  // The capture is a young store over a 30-day request: partial window,
  // and a period narrower than the one asked for.
  expect(screen.getByText("Partial period")).toBeInTheDocument();
  expect(screen.getByText("Coverage limited")).toHaveTextContent("2026-09-02");
});
