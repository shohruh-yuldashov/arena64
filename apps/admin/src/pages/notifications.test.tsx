import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, expect, it, vi } from "vitest";

import { App } from "@/app/App";
import { createAdminRouter } from "@/app/router";
import { accessToken } from "@/app/session-store";

/**
 * The Notification Operations console — A64-024.7 §28.
 *
 * Five tests through the real router. Each asserts something an operator
 * could not detect by looking: that the console never claims a delivery the
 * platform cannot observe, that an ineligible device offers no action, that
 * the retry confirms and sends no body, and that a refusal leaves the page
 * telling the truth about what happened.
 */

const ADMIN = { id: "a", username: "op", display_name: "Op", roles: ["admin"] };

const delivery = (overrides: Record<string, unknown> = {}) => ({
  subscription_id: "11111111-1111-1111-1111-111111111111",
  status: "failed",
  outcome: "attempts_exhausted",
  attempt_count: 5,
  next_attempt_at: null,
  last_attempt_at: "2026-08-09T12:00:00Z",
  accepted_at: null,
  created_at: "2026-08-09T11:00:00Z",
  can_retry: true,
  device_first_seen_at: "2026-07-01T00:00:00Z",
  device_last_seen_at: "2026-08-09T10:00:00Z",
  device_revoked_at: null,
  ...overrides,
});

const summary = (overrides: Record<string, unknown> = {}) => ({
  id: "n-1",
  recipient_id: "u-1",
  recipient_username: "player",
  type: "tournament_round_published",
  category: "tournament",
  created_at: "2026-08-09T12:00:00Z",
  read_at: null,
  push_capable: true,
  push_summary: "failed",
  delivery_count: 1,
  ...overrides,
});

const detail = (overrides: Record<string, unknown> = {}) => ({
  id: "n-1",
  recipient_id: "u-1",
  recipient_username: "player",
  type: "tournament_round_published",
  category: "tournament",
  target_type: "tournament",
  target_ref: "t-1",
  source_event_id: "e-1",
  created_at: "2026-08-09T12:00:00Z",
  read_at: null,
  push_capable: true,
  deliveries: [delivery()],
  ...overrides,
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

interface Stub {
  posts: { url: string; body: string | null }[];
  gets: string[];
  retryStatus: number;
  retryResponse: unknown;
  detail: Record<string, unknown>;
  items: unknown[];
}

function stubApi(overrides: Partial<Stub> = {}): Stub {
  const stub: Stub = {
    posts: [],
    gets: [],
    retryStatus: 200,
    retryResponse: delivery({ status: "pending", outcome: null, can_retry: false }),
    detail: detail(),
    items: [summary()],
    ...overrides,
  };

  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    if (url.endsWith("/admin/me")) return Promise.resolve(json({ data: ADMIN }));

    if (init?.method === "POST") {
      stub.posts.push({ url, body: (init.body as string | undefined) ?? null });
      return Promise.resolve(json({ data: stub.retryResponse }, stub.retryStatus));
    }

    if (url.includes("/admin/notifications/")) {
      stub.gets.push(url);
      return Promise.resolve(json({ data: stub.detail }));
    }
    if (url.includes("/admin/notifications")) {
      stub.gets.push(url);
      return Promise.resolve(json({ data: { items: stub.items, next_cursor: null } }));
    }
    return Promise.resolve(json({}, 404));
  });
  return stub;
}

function renderAt(path: string) {
  const router = createAdminRouter(createMemoryHistory({ initialEntries: [path] }));
  render(<App router={router} />);
  return router;
}

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.open = false;
  };
});

afterEach(() => {
  vi.unstubAllGlobals();
  accessToken.clear();
});

it("never claims a push was delivered, and cannot send from the delivery console", async () => {
  // Web Push reports that a service accepted a request and nothing more, so
  // "Delivered" would be a claim the platform cannot support — an operator
  // would use it to close an investigation that should stay open.
  //
  // A64-027A added a composer to this workspace, on its own tab. The
  // delivery console still has none: the retry route takes two identifiers
  // and no body, and a send control beside a failed delivery would be one
  // clicked without reading which delivery it was.
  stubApi({ items: [summary({ push_summary: "sent" })] });
  renderAt("/notifications?tab=deliveries");

  const table = await screen.findByRole("table");
  expect(
    within(table).getAllByText(/qabul qildi|Принято push-сервисом|Accepted by push service/)
      .length,
  ).toBeGreaterThan(0);
  expect(within(table).queryByText(/^Delivered$/)).not.toBeInTheDocument();

  const panel = document.getElementById("panel-deliveries") as HTMLElement;
  expect(
    within(panel).queryByRole("button", { name: /send|yubor|отправ/i }),
  ).not.toBeInTheDocument();
});

it("sends the failed-push filter and resets the accumulated rows", async () => {
  const stub = stubApi();
  const person = userEvent.setup();
  const router = renderAt("/notifications?tab=deliveries");

  await screen.findByRole("table");
  await person.selectOptions(screen.getByLabelText(/Ko'rinish|Вид|View/), "failed");

  await waitFor(() =>
    expect(stub.gets.some((url) => url.includes("failed_push_only=true"))).toBe(true),
  );
  expect(router.state.location.search).toMatchObject({ failed: "true" });
});

it("offers no retry for a device the server says is ineligible", async () => {
  // `can_retry` is the server's, computed from the same rule its guarded
  // UPDATE enforces — a muted recipient or a dead subscription must not even
  // be asked about, because the answer would be a refusal every time.
  stubApi({
    detail: detail({
      deliveries: [
        delivery({ status: "skipped", outcome: "skipped_preference", can_retry: false }),
      ],
    }),
  });
  renderAt("/notifications/n-1");

  // A64-027A.4 translates the notification type. The heading names the
  // thing that happened, not the enum that records it.
  await screen.findByRole("heading", {
    name: /Turnir raundi|Раунд турнира|Tournament round paired/,
  });
  expect(
    screen.getByText(/Qayta urinib bo'lmaydi|Повтор недоступен|Retry unavailable/),
  ).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Qayta urinish|Повторить|Retry/ })).toBeNull();
  // And the outcome is a localised phrase, never a provider's own text.
  expect(
    screen.getAllByText(
      /Foydalanuvchi o'chirgan|Отключено пользователем|Muted by the recipient/,
    ).length,
  ).toBeGreaterThan(0);
});

it("confirms the retry, sends no body, and reports only that it was queued", async () => {
  // The response is the delivery's new state: queued. The console says that
  // and nothing stronger — claiming the push succeeded would be inventing an
  // acknowledgement the platform never receives.
  const stub = stubApi();
  const person = userEvent.setup();
  renderAt("/notifications/n-1");

  await person.click(
    await screen.findByRole("button", { name: /Qayta urinish|Повторить|Retry/ }),
  );
  expect(stub.posts).toHaveLength(0);

  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/player/)).toBeInTheDocument();
  await person.click(
    within(dialog).getByRole("button", { name: /Qayta urinish|Повторить|Retry/ }),
  );

  await waitFor(() => expect(stub.posts).toHaveLength(1));
  expect(stub.posts[0]?.url).toContain(
    "/admin/notifications/n-1/deliveries/11111111-1111-1111-1111-111111111111/retry",
  );
  expect(stub.posts[0]?.body).toBe("{}");

  await screen.findByText(/navbatga qo'yildi|поставлена в очередь|queued again/);
  expect(screen.queryByText(/^Delivered$/)).not.toBeInTheDocument();
});

it("keeps the page honest when the server refuses the retry", async () => {
  // A 409 means the row is already queued or was never eligible. The console
  // must not show it as retried, and must not close the dialog on a failure
  // the operator has to read.
  const stub = stubApi({ retryStatus: 409, retryResponse: { detail: "no" } });
  const person = userEvent.setup();
  renderAt("/notifications/n-1");

  await person.click(
    await screen.findByRole("button", { name: /Qayta urinish|Повторить|Retry/ }),
  );
  const dialog = await screen.findByRole("dialog");
  await person.click(
    within(dialog).getByRole("button", { name: /Qayta urinish|Повторить|Retry/ }),
  );

  await within(dialog).findByRole("alert");
  expect(stub.posts).toHaveLength(1);
  expect(screen.queryByText(/navbatga qo'yildi|поставлена в очередь|queued again/)).toBeNull();
});
