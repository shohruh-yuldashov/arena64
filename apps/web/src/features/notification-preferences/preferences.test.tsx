import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";

import { env } from "@/shared/config/env";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * The notification preference screen, through the real app — A64-021.3 §32.
 *
 * Three tests, mounting the **real router** at `/settings/notifications`, so
 * route registration, the guard, the shell, the query layer and the mutation
 * are exercised together. §33: a component rendered in isolation is not a
 * reachability proof. What is substituted is the HTTP layer and nothing else.
 *
 * The three claims are the ones a settings screen can silently get wrong:
 *
 *   a control nobody may use **says why**, in words, rather than being a
 *   greyed box the player has to guess about
 *
 *   a save sends **only what moved** — a client that posted the whole grid
 *   would overwrite categories it never rendered
 *
 *   a refused save shows the refusal's **own** message and keeps the
 *   player's pending changes, rather than a generic error and lost work
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

const CATEGORIES = ["social", "game", "tournament", "system"] as const;

/** The matrix the backend actually serves a player who has chosen nothing. */
function defaultSettings() {
  return CATEGORIES.flatMap((category) => [
    {
      category,
      channel: "in_app",
      enabled: true,
      available: true,
      editable: category !== "system",
      locked_reason: category === "system" ? "essential" : null,
    },
    ...(["email", "push"] as const).map((channel) => ({
      category,
      channel,
      enabled: false,
      available: false,
      editable: false,
      locked_reason: "channel_unavailable",
    })),
  ]);
}

/** Every preference body the app sent, so "only what moved" is an assertion. */
let saved: unknown[] = [];

function signedIn(): void {
  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: VIEWER })),
    ),
  );
}

function servePreferences(): void {
  mswServer.use(
    http.get(url("/notifications/preferences"), () =>
      HttpResponse.json(envelope({ settings: defaultSettings() })),
    ),
  );
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  saved = [];
  signedIn();
  servePreferences();
});

it("reflects an available email channel instead of claiming it is not", async () => {
  // A64-021.5 §26, §31.12. The server decides whether email delivers, and
  // the screen must agree with it in **both** directions: the earlier test
  // covers the unavailable state, this one covers the day a provider is
  // configured.
  //
  // The failure this prevents is a hardcoded "not available yet" hint that
  // keeps saying so after the channel works — the same lie as offering a
  // switch that does nothing, pointing the other way.
  mswServer.use(
    http.get(url("/notifications/preferences"), () =>
      HttpResponse.json(
        envelope({
          settings: defaultSettings().map((setting) =>
            setting.channel === "email"
              ? { ...setting, available: true, editable: true, locked_reason: null }
              : setting,
          ),
        }),
      ),
    ),
  );
  renderApp({ path: "/settings/notifications" });

  const social = await screen.findByRole("group", { name: /friends and social/i });
  const email = within(social).getByRole("checkbox", { name: /email/i });
  expect(email).toBeEnabled();
  expect(within(social).queryByText(/not available yet/i)).not.toBeInTheDocument();
  // And the one thing a player has to know before turning it on. Stated in
  // the channel key above the grid rather than beside each email switch —
  // A64-025.9C — so `getAllByText` with a length of one is the assertion:
  // the caveat is still on the page *and* is no longer printed once per
  // category. Whether the address is verified is a property of the account,
  // not of tournaments-versus-friends.
  expect(
    screen.getAllByText(/only a verified email address receives notifications/i),
  ).toHaveLength(1);
});

it("explains every control a player may not change", async () => {
  renderApp({ path: "/settings/notifications" });

  // The essential lock and the unbuilt channel are **different sentences**.
  // A screen that greyed both out identically would tell a player that push
  // notifications are forbidden, when the truth is that they do not exist.
  expect(
    await screen.findAllByText(/we must be able to tell you about your account/i),
  ).not.toHaveLength(0);
  expect(screen.getAllByText(/this build cannot send here yet/i)).not.toHaveLength(0);

  // And the locks are real, not decorative.
  const emails = screen.getAllByRole("checkbox", { name: /email/i });
  expect(emails.every((box) => (box as HTMLInputElement).disabled)).toBe(true);
});

it("sends only the switches that moved", async () => {
  mswServer.use(
    http.patch(url("/notifications/preferences"), async ({ request }) => {
      saved.push(await request.json());
      const settings = defaultSettings().map((setting) =>
        setting.category === "social" && setting.channel === "in_app"
          ? { ...setting, enabled: false }
          : setting,
      );
      return HttpResponse.json(envelope({ settings }));
    }),
  );
  renderApp({ path: "/settings/notifications" });

  const social = await screen.findByRole("group", { name: /friends and social/i });
  await userEvent.click(within(social).getByRole("checkbox", { name: /in the app/i }));
  await userEvent.click(screen.getByRole("button", { name: /save changes/i }));

  await waitFor(() => expect(saved).toHaveLength(1));
  // One entry, not twelve: the three other categories were never touched
  // and must not appear in the body at all.
  expect(saved[0]).toEqual({
    changes: [{ category: "social", channel: "in_app", enabled: false }],
  });
  expect(await screen.findByText(/preferences saved/i)).toBeInTheDocument();
});

it("shows the refusal's own message and keeps the pending change", async () => {
  mswServer.use(
    http.patch(url("/notifications/preferences"), () =>
      HttpResponse.json(
        { code: "notification_channel_unavailable", message: "no", request_id: null },
        { status: 422 },
      ),
    ),
  );
  renderApp({ path: "/settings/notifications" });

  const game = await screen.findByRole("group", { name: /games/i });
  await userEvent.click(within(game).getByRole("checkbox", { name: /in the app/i }));
  await userEvent.click(screen.getByRole("button", { name: /save changes/i }));

  expect(await screen.findByText(/that channel is not available yet/i)).toBeInTheDocument();
  // The work is not thrown away: the count still stands, so the player can
  // fix the one offending switch rather than redo the whole screen.
  expect(screen.getByText(/1 unsaved/i)).toBeInTheDocument();
});
