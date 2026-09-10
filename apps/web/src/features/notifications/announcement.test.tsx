import { screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";

import { env } from "@/shared/config/env";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * A platform announcement is legible where it lands — A64-031.D.
 *
 * The defect: announcements were delivered, counted and shown, and what a
 * player read was "New notification". `notificationMessage` had no branch
 * for `platform_announcement`, so it fell through to the generic sentence —
 * correct for a type this build has never heard of, wrong for one it ships.
 *
 * Through the **real router** at `/notifications`, like the coverage suite
 * beside it: what is substituted is the HTTP layer and nothing else, so the
 * query layer, the mapper and the row are exercised together.
 *
 * ## Why the escaping test is here and not in a unit test
 *
 * An announcement is the one payload carrying text a human wrote, which
 * makes "could an operator put markup on another player's screen" a real
 * question rather than a theoretical one. It is answered by React rendering
 * the string as a text child — so the assertion has to be made against a
 * real DOM, where `innerHTML` can be read back.
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

function announcement(overrides: Record<string, unknown> = {}) {
  return {
    id: "019fe400-0000-7000-8000-000000000009",
    type: "platform_announcement",
    category: "announcement",
    actor: null,
    tournament: null,
    game: null,
    challenge: null,
    announcement: {
      title: "Scheduled maintenance",
      body: "The platform is unavailable from 02:00 to 04:00 UTC.",
      locale: "en",
    },
    // `home`, which `notificationHref` resolves to no link at all — the
    // announcement *is* the row, so there is nowhere for a tap to go.
    target: { type: "home", ref: null },
    created_at: "2026-09-10T09:00:00Z",
    read_at: null,
    is_read: false,
    ...overrides,
  };
}

function signedIn(): void {
  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: VIEWER })),
    ),
  );
}

function serve(entries: unknown[]): void {
  mswServer.use(
    http.get(url("/notifications"), () =>
      HttpResponse.json(envelope({ entries, next_cursor: null })),
    ),
    http.get(url("/notifications/unread-count"), () =>
      HttpResponse.json(envelope({ unread_count: entries.length })),
    ),
  );
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  signedIn();
});

it("shows the operator's title and body rather than the generic sentence", async () => {
  serve([announcement()]);

  renderApp({ path: "/notifications" });

  const list = await screen.findByRole("list", { name: /notifications/i });
  expect(await within(list).findByText("Scheduled maintenance")).toBeInTheDocument();
  expect(
    within(list).getByText("The platform is unavailable from 02:00 to 04:00 UTC."),
  ).toBeInTheDocument();
  // The exact failure this file exists for.
  expect(within(list).queryByText("New notification")).not.toBeInTheDocument();
});

it("marks the text with the language it was written in", async () => {
  // Not the language of the interface. Without `lang`, a screen reader
  // pronounces Russian prose with the interface's rules — WCAG 3.1.2.
  serve([
    announcement({
      announcement: {
        title: "Плановое обслуживание",
        body: "Платформа недоступна с 02:00 до 04:00 UTC.",
        locale: "ru",
      },
    }),
  ]);

  renderApp({ path: "/notifications" });

  const title = await screen.findByText("Плановое обслуживание");
  expect(title.closest("[lang]")).toHaveAttribute("lang", "ru");
});

it("renders an operator's angle brackets as text, never as markup", async () => {
  serve([
    announcement({
      announcement: {
        title: "Rules update",
        body: '<img src=x onerror="alert(1)"> and the 5 < 7 rule',
        locale: "en",
      },
    }),
  ]);

  renderApp({ path: "/notifications" });

  const list = await screen.findByRole("list", { name: /notifications/i });
  // Present as characters the reader sees...
  expect(
    await within(list).findByText('<img src=x onerror="alert(1)"> and the 5 < 7 rule'),
  ).toBeInTheDocument();
  // ...and absent as an element. `querySelector` rather than a text match:
  // this is the assertion that a text child was rendered and not parsed.
  expect(list.querySelector("img")).toBeNull();
  expect(list.innerHTML).toContain("&lt;img");
});

it("falls back to the generic sentence when the payload is missing", async () => {
  // A type this build knows, arriving without the key it promises, is
  // malformed — and two empty lines is a worse answer than a short one.
  serve([announcement({ announcement: null })]);

  renderApp({ path: "/notifications" });

  const list = await screen.findByRole("list", { name: /notifications/i });
  expect(await within(list).findByText("New notification")).toBeInTheDocument();
});

it("renders the announcement as a non-navigable row", async () => {
  // `home` has no `notificationHref`, and §6 says a target a client cannot
  // resolve renders as a plain row rather than a link that goes nowhere.
  serve([announcement()]);

  renderApp({ path: "/notifications" });

  const list = await screen.findByRole("list", { name: /notifications/i });
  await within(list).findByText("Scheduled maintenance");
  expect(within(list).queryByRole("link")).not.toBeInTheDocument();
});
