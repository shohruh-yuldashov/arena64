import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";

import { env } from "@/shared/config/env";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * The notification surface, through the real app — A64-021.1 §32.7–§32.9.
 *
 * Every test mounts the **real router** at a real path, so route
 * registration, the guard, the header, the query layer and the mutations
 * are exercised together. §33 is explicit that a component rendered in
 * isolation is not a reachability proof; what is substituted here is the
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

const ACTOR = {
  player_id: "019fb9ea-0a0c-7cec-9c5f-402727c31b01",
  username: "rival",
  display_name: "Rival",
  thumbnail_url: null,
};

function notification(overrides: Record<string, unknown> = {}) {
  return {
    id: "019fe400-0000-7000-8000-000000000001",
    type: "friend_request_received",
    category: "social",
    actor: ACTOR,
    target: { type: "friend_requests", ref: null },
    created_at: "2026-08-05T10:00:00Z",
    read_at: null,
    is_read: false,
    ...overrides,
  };
}

/** Every request the app made, so an N+1 is an assertion rather than a hope. */
let requests: string[] = [];

function signedIn(): void {
  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: VIEWER })),
    ),
  );
}

function signedOut(): void {
  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json({ code: "unauthorized", message: "No." }, { status: 401 }),
    ),
  );
}

function serveUnreadCount(count: number): void {
  mswServer.use(
    http.get(url("/notifications/unread-count"), () => {
      requests.push("unread-count");
      return HttpResponse.json(envelope({ unread_count: count }));
    }),
  );
}

/** Two pages, chained by an opaque cursor the client must send back verbatim. */
function serveTwoPages(): void {
  mswServer.use(
    http.get(url("/notifications"), ({ request }) => {
      const after = new URL(request.url).searchParams.get("after");
      requests.push(`list:${after ?? "first"}`);
      if (after === "cursor-1") {
        return HttpResponse.json(
          envelope({
            entries: [
              notification({
                id: "019fe400-0000-7000-8000-000000000003",
                type: "friend_request_accepted",
                target: { type: "player_profile", ref: "rival" },
                is_read: true,
                read_at: "2026-08-05T11:00:00Z",
              }),
            ],
            next_cursor: null,
          }),
        );
      }
      return HttpResponse.json(
        envelope({
          entries: [
            notification(),
            notification({ id: "019fe400-0000-7000-8000-000000000002" }),
          ],
          next_cursor: "cursor-1",
        }),
      );
    }),
  );
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  requests = [];
  // Any per-row profile read is a failure of §31 — the actor arrives
  // composed on the notification itself.
  mswServer.use(
    http.get(url("/profiles/:username"), () => {
      requests.push("profile");
      return HttpResponse.json(envelope({}));
    }),
    http.get(url("/users/:id"), () => {
      requests.push("profile");
      return HttpResponse.json(envelope({}));
    }),
  );
});

it("protects /notifications and reaches it from the real application shell", async () => {
  // Signed out: the guard sends the visitor to sign in and keeps where they
  // were going, and the bell is absent rather than disabled.
  signedOut();
  const anonymous = renderApp({ path: "/notifications" });
  // The guard replaced the page rather than rendering it behind a spinner.
  expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeVisible();
  expect(
    screen.queryByRole("heading", { level: 1, name: "Notifications" }),
  ).not.toBeInTheDocument();
  // And the bell is absent rather than disabled: there is no anonymous
  // notification list to link to.
  expect(screen.queryByRole("link", { name: /Notifications/ })).not.toBeInTheDocument();
  anonymous.unmount();

  // Signed in: the header carries the one entry point, and following it
  // renders the page — which is the reachability §33 asks for, through the
  // real `AppShell` and the real route tree.
  signedIn();
  serveUnreadCount(0);
  serveTwoPages();
  renderApp({ path: "/" });

  const bell = await screen.findByRole("link", { name: "Notifications" });
  await userEvent.click(bell);

  expect(await screen.findByRole("heading", { level: 1, name: "Notifications" })).toBeVisible();
});

it("reads one page per request and never a profile per row", async () => {
  signedIn();
  serveUnreadCount(2);
  serveTwoPages();
  renderApp({ path: "/notifications" });

  const list = await screen.findByRole("list", { name: "Notifications" });
  await waitFor(() => expect(within(list).getAllByRole("listitem")).toHaveLength(2));

  // §31: one request for the page, and **zero** per row. The actor's name
  // and avatar were stored with the notification.
  expect(requests.filter((entry) => entry.startsWith("list:"))).toEqual(["list:first"]);
  expect(requests).not.toContain("profile");

  await userEvent.click(screen.getByRole("button", { name: "Load more" }));

  // The cursor is sent back verbatim — never decoded, never turned into an
  // offset — and the second page is one more request, not a refetch of the
  // first.
  await waitFor(() =>
    expect(requests.filter((entry) => entry.startsWith("list:"))).toEqual([
      "list:first",
      "list:cursor-1",
    ]),
  );
  expect(within(await screen.findByRole("list")).getAllByRole("listitem")).toHaveLength(3);
  expect(requests).not.toContain("profile");

  // The last page says so rather than offering a button that fetches nothing.
  expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  expect(screen.getByText("No more notifications")).toBeVisible();
});

it("announces the unread count and marks one read exactly once", async () => {
  const marked: string[] = [];
  signedIn();
  serveTwoPages();
  let unread = 2;
  mswServer.use(
    http.get(url("/notifications/unread-count"), () =>
      HttpResponse.json(envelope({ unread_count: unread })),
    ),
    http.post(url("/notifications/:id/read"), ({ params }) => {
      marked.push(String(params.id));
      unread -= 1;
      return HttpResponse.json(envelope({ marked_read: 1 }));
    }),
  );

  renderApp({ path: "/notifications" });

  // §18, §28: the count has a textual equivalent in the control's own
  // accessible name — a screen reader never has to read a bare number out
  // of a coloured circle.
  const bell = await screen.findByRole("link", { name: "Notifications — 2 unread" });
  expect(bell).toBeVisible();

  // §28: unread is not colour alone. Every unread row carries the word.
  const list = await screen.findByRole("list", { name: "Notifications" });
  const rows = within(list).getAllByRole("listitem");
  expect(within(rows[0]!).getByText("Unread")).toBeInTheDocument();

  // §21: a double click is one mutation.
  //
  // `fireEvent` twice with **no await between**, which is the case the
  // guard exists for: `userEvent.click` awaits, so the first mutation would
  // settle and the second click would be an ordinary click on an
  // already-read row. Two clicks in one tick is a real double tap, and the
  // in-flight guard is what stops it becoming two requests.
  const link = within(rows[0]!).getByRole("link");
  fireEvent.click(link);
  fireEvent.click(link);

  await waitFor(() => expect(marked).toEqual(["019fe400-0000-7000-8000-000000000001"]));

  // The badge reconciles with the server rather than being decremented
  // locally, so it stays correct if another device read something too.
  await waitFor(() =>
    expect(screen.getByRole("link", { name: "Notifications — 1 unread" })).toBeVisible(),
  );
});
