import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { App } from "@/app/App";
import { createAdminRouter } from "@/app/router";
import { accessToken } from "@/app/session-store";

/**
 * Analytics and Notifications, as a contract — A64-027A.4 §46.
 *
 * The visualisations added in this task are all `value / max` widths and
 * tinted cells, and every one of them is a place where a `?? 0` would turn
 * "not measured" into a number. These tests pin the handful of decisions
 * that keep the pages honest, and nothing about their CSS.
 */

const ADMIN = { id: "a", username: "op", display_name: "Operator", roles: ["admin"] };

const meta = (overrides: Record<string, unknown> = {}) => ({
  environment: "production",
  include_synthetic: false,
  period_start: "2026-08-06",
  period_end: "2026-09-04",
  requested_start: "2026-08-06",
  requested_end: "2026-09-04",
  maturity: "mature",
  coverage: "complete",
  generated_at: "2026-09-05T09:30:00Z",
  ...overrides,
});

const overview = (overrides: Record<string, unknown> = {}) => ({
  active_players: {
    as_of: "2026-09-04",
    daily: 120,
    weekly: 500,
    monthly: 1200,
    stickiness: 0.1,
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
        stage: "activated",
        subjects: 120,
        conversion_from_previous: 0.3,
        conversion_from_start: 0.3,
        drop_off: 280,
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
    ],
    meta: meta(),
  },
  engagement: {
    week_start: "2026-08-24",
    week_end: "2026-08-30",
    active_players: 500,
    match_starts: 1200,
    matches_per_active_player: 2.4,
    median_matches_per_active_player: 2,
    tournament_entrants: 60,
    tournament_participation: 0.12,
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

const retention = (rows: unknown[]) => ({ rows, meta: meta() });

const acquisition = (overrides: Record<string, unknown> = {}) => ({
  stages: [
    {
      stage: "landing_viewed",
      subjects: 1000,
      conversion_from_previous: null,
      conversion_from_start: null,
      drop_off: 0,
    },
    {
      stage: "user_registered",
      subjects: 50,
      conversion_from_previous: 0.05,
      conversion_from_start: 0.05,
      drop_off: 950,
    },
  ],
  overall_conversion: 0.05,
  registrations_in_range: 400,
  meta: meta(),
  ...overrides,
});

const broadcast = (overrides: Record<string, unknown> = {}) => ({
  id: "b1",
  title: "Texnik ishlar",
  body: "Bugun kechqurun.",
  locale: "uz",
  audience: "all_players",
  channel: "in_app",
  status: "completed",
  created_at: "2026-09-04T18:00:00Z",
  started_at: "2026-09-04T18:00:05Z",
  completed_at: "2026-09-04T18:02:00Z",
  audience_size: 12548,
  delivered: 11982,
  named_recipients: 0,
  failure_reason: null,
  ...overrides,
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

interface Stub {
  size: number | null;
  history: unknown[];
  overview: Record<string, unknown>;
  retention: unknown[];
  acquisition: Record<string, unknown>;
}

function stubApi(overrides: Partial<Stub> = {}) {
  const stub: Stub = {
    size: 12548,
    history: [],
    overview: overview(),
    retention: [],
    acquisition: acquisition(),
    ...overrides,
  };
  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    if (url.endsWith("/admin/me")) return Promise.resolve(json({ data: ADMIN }));
    if (url.includes("analytics/overview"))
      return Promise.resolve(json({ data: stub.overview }));
    if (url.includes("analytics/retention")) {
      return Promise.resolve(json({ data: retention(stub.retention) }));
    }
    if (url.includes("analytics/acquisition")) {
      return Promise.resolve(json({ data: stub.acquisition }));
    }
    if (url.includes("/admin/broadcasts/audience/")) {
      if (stub.size === null) return Promise.resolve(json({}, 503));
      return Promise.resolve(json({ data: { audience: "all_players", size: stub.size } }));
    }
    if (url.includes("/admin/broadcasts")) {
      return Promise.resolve(json({ data: { items: stub.history } }));
    }
    return Promise.resolve(json({}, 404));
  });
  return stub;
}

function renderAt(path: string) {
  render(<App router={createAdminRouter(createMemoryHistory({ initialEntries: [path] }))} />);
}

afterEach(() => {
  vi.unstubAllGlobals();
  accessToken.clear();
});

/** §46 A, C — an unmeasured cohort is not a churned one. */
it("renders an unmeasured retention cell as a dash with no tint", async () => {
  stubApi({
    retention: [
      {
        cohort_day: "2026-09-01",
        cohort: 100,
        d1: 40,
        d7: 20,
        d30: 5,
        d1_rate: 0.4,
        d7_rate: 0.2,
        d30_rate: 0.05,
      },
      {
        cohort_day: "2026-09-04",
        cohort: 80,
        d1: null,
        d7: null,
        d30: null,
        d1_rate: null,
        d7_rate: null,
        d30_rate: null,
      },
    ],
  });
  renderAt("/analytics");

  const immature = (await screen.findByRole("rowheader", { name: "2026-09-04" })).closest("tr");
  expect(immature).not.toBeNull();

  const cells = within(immature as HTMLElement).getAllByRole("cell");
  // [size, D1, D7, D30] — the three rates are unmeasured.
  for (const cell of cells.slice(1)) {
    expect(cell).toHaveAttribute("data-unmeasured", "true");
    expect(cell.querySelector(".cohort__tint")).toBeNull();
  }
  expect(immature?.textContent).not.toContain("0%");

  // A measured cohort carries a tint, so the two states are distinguishable
  // by more than the number.
  const measured = screen.getByRole("rowheader", { name: "2026-09-01" }).closest("tr");
  expect((measured as HTMLElement).querySelector(".cohort__tint")).not.toBeNull();
});

/** §46 D — every bar width comes from a returned value. */
it("draws funnel bars from the server's own subject counts", async () => {
  stubApi();
  renderAt("/analytics");

  await screen.findAllByText(/Viewed the landing page/);
  const fills = document.querySelectorAll(".funnel__fill");
  expect(fills.length).toBeGreaterThanOrEqual(2);

  // 1000/1000 and 50/1000 — the first stage is the denominator.
  expect((fills[0] as HTMLElement).style.inlineSize).toBe("100%");
  expect((fills[1] as HTMLElement).style.inlineSize).toBe("5%");
});

/** §46 B — the page renders rates, it does not compute them. */
it("shows the server's conversion, not one divided from the counts", async () => {
  stubApi({
    acquisition: acquisition({
      stages: [
        {
          stage: "landing_viewed",
          subjects: 1000,
          conversion_from_previous: null,
          conversion_from_start: null,
          drop_off: 0,
        },
        // Deliberately not 50/1000: if the page divides it prints 5%.
        {
          stage: "user_registered",
          subjects: 50,
          conversion_from_previous: 0.42,
          conversion_from_start: 0.37,
          drop_off: 950,
        },
      ],
    }),
  });
  renderAt("/analytics");

  await screen.findAllByText(/Registered/);

  // Scoped to the **bars**, not the page: the accessible table below them
  // renders the same rates from the same fields, so a body-wide assertion
  // passes even when the bars divide their own numbers. Both halves are
  // checked, separately.
  const rates = document.querySelectorAll(".funnel__rates");
  const barText = Array.from(rates)
    .map((node) => node.textContent ?? "")
    .join(" ");
  expect(barText).toContain("42%");
  expect(barText).toContain("37%");
  // 50/1000 is what a page that computed its own conversion would print.
  expect(barText).not.toContain("5%");

  const table = document.querySelector(".chart-table table");
  expect(table?.textContent ?? "").toContain("42%");
});

/** §46 E — the coverage notice survives the redesign. */
it("keeps the partial-period notice visible", async () => {
  stubApi({ overview: overview({ meta: meta({ maturity: "partial" }) }) });
  renderAt("/analytics");

  expect(await screen.findByText(/Partial period/)).toBeInTheDocument();
});

/** §46 F — the recipient count is the server's or it is unknown. */
it("says the recipient count is unavailable rather than showing zero", async () => {
  stubApi({ size: null });
  renderAt("/notifications?tab=send");

  expect(
    await screen.findByText(/could not be fetched|olishning iloji|не удалось получить/i),
  ).toBeInTheDocument();
  expect(screen.queryByText(/^0$/)).not.toBeInTheDocument();
});

/** §46 G — one channel exists, and only one is offered. */
it("offers no channel the platform does not have", async () => {
  stubApi();
  renderAt("/notifications?tab=send");

  await screen.findAllByText(/12,548/);
  const text = document.body.textContent ?? "";
  for (const absent of [/\bEmail\b/, /\bPush\b/, /\bSMS\b/]) {
    expect(text).not.toMatch(absent);
  }
});

/** §46 I — no URL field, ever. */
it("exposes no link or image input in the composer", async () => {
  stubApi();
  renderAt("/notifications?tab=send");

  await screen.findAllByText(/12,548/);
  const main = document.querySelector("main") as HTMLElement;
  expect(main.querySelector('input[type="url"]')).toBeNull();
  expect(main.querySelector('input[type="file"]')).toBeNull();
  for (const label of [/url/i, /link/i, /image/i]) {
    expect(within(main).queryByLabelText(label)).not.toBeInTheDocument();
  }
});

/** §46 J — the delivery listing names the event, not the enum. */
it("names the notification type rather than its identifier", async () => {
  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    if (url.endsWith("/admin/me")) return Promise.resolve(json({ data: ADMIN }));
    if (url.includes("/admin/notifications")) {
      return Promise.resolve(
        json({
          data: {
            items: [
              {
                id: "n1",
                recipient_id: "u1",
                recipient_username: "dilnoza",
                type: "tournament_round_published",
                category: "tournament",
                created_at: "2026-09-05T09:00:00Z",
                read_at: null,
                push_capable: true,
                push_summary: "sent",
                delivery_count: 2,
              },
            ],
            next_cursor: null,
          },
        }),
      );
    }
    return Promise.resolve(json({}, 404));
  });
  renderAt("/notifications?tab=deliveries");

  const table = await screen.findByRole("table");
  expect(
    within(table).getByText(/Turnir raundi|Раунд турнира|Tournament round paired/),
  ).toBeInTheDocument();
  expect(within(table).queryByText("tournament_round_published")).not.toBeInTheDocument();
});

/** §46 K — push acceptance is never called delivery. */
it("never claims a push reached a device", async () => {
  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    if (url.endsWith("/admin/me")) return Promise.resolve(json({ data: ADMIN }));
    if (url.includes("/admin/notifications")) {
      return Promise.resolve(
        json({
          data: {
            items: [
              {
                id: "n1",
                recipient_id: "u1",
                recipient_username: "dilnoza",
                type: "game_completed",
                category: "game",
                created_at: "2026-09-05T09:00:00Z",
                read_at: null,
                push_capable: true,
                push_summary: "sent",
                delivery_count: 1,
              },
            ],
            next_cursor: null,
          },
        }),
      );
    }
    return Promise.resolve(json({}, 404));
  });
  renderAt("/notifications?tab=deliveries");

  const table = await screen.findByRole("table");
  // "Accepted by the push service" — the strongest truthful phrase.
  expect(
    within(table).getByText(/qabul qildi|Принято push-сервисом|Accepted by push service/),
  ).toBeInTheDocument();
  expect(within(table).queryByText(/^Delivered$/)).not.toBeInTheDocument();
  expect(within(table).queryByText(/^Read$/)).not.toBeInTheDocument();
});

/** The history's progress bar is a ratio of two counts, never invented. */
it("draws delivery progress only when the denominator exists", async () => {
  stubApi({
    history: [
      broadcast({ id: "b1", delivered: 11982, audience_size: 12548 }),
      broadcast({ id: "b2", status: "queued", delivered: 0, audience_size: null }),
    ],
  });
  renderAt("/notifications?tab=history");

  const table = await screen.findByRole("table");
  const fills = table.querySelectorAll(".delivered__fill");
  // Only the counted broadcast has a bar; the uncounted one has none.
  expect(fills).toHaveLength(1);
  expect((fills[0] as HTMLElement).style.inlineSize).toMatch(/^95\.4/);
  expect(within(table).getByText(/counting|hisoblanmoqda|подсчёт/i)).toBeInTheDocument();
});
