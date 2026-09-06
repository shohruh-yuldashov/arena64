import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";

import type { SessionRead } from "@/features/sessions/api";
import { env } from "@/shared/config/env";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * The Active Sessions screen, through the real app — A64-030.5C, SE-2.
 *
 * Mounted at `/settings/sessions` with the real router, guard, shell and
 * query layer; only the HTTP boundary is substituted. A component rendered
 * in isolation would prove that it renders, not that a player can reach it.
 *
 * The claims are the ones a device list can silently get wrong:
 *
 *   the current device is **marked and not revocable**, because a button
 *   there would sign the page out fifteen minutes later for no visible
 *   reason;
 *
 *   an unrecognised client degrades to "Unknown browser", rather than to a
 *   confident guess that defeats the only purpose the screen has;
 *
 *   a revoke actually **re-reads the list** rather than removing the row
 *   locally, so a device that was not signed out cannot appear to have
 *   been.
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

const LAPTOP: SessionRead = {
  id: "019fb9ea-0a0c-7cec-9c5f-402727c31a01",
  is_current: true,
  browser: "Chrome",
  platform: "macOS",
  signed_in_at: "2026-09-01T10:00:00Z",
  last_active_at: "2026-09-07T09:00:00Z",
};

const PHONE: SessionRead = {
  id: "019fb9ea-0a0c-7cec-9c5f-402727c31a02",
  is_current: false,
  browser: "Safari",
  platform: "iPhone",
  signed_in_at: "2026-08-20T08:00:00Z",
  last_active_at: "2026-09-07T07:00:00Z",
};

/** Every device id the app asked the server to revoke. */
let revoked: string[] = [];
/** How many times the list was read, so "it refetches" is an assertion. */
let listReads = 0;

function signedIn(): void {
  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: VIEWER })),
    ),
  );
}

function serveDevices(...pages: SessionRead[][]): void {
  mswServer.use(
    http.get(url("/auth/browser/sessions"), () => {
      const page = pages[Math.min(listReads, pages.length - 1)];
      listReads += 1;
      return HttpResponse.json(envelope(page));
    }),
  );
}

/**
 * The device rows, and only those.
 *
 * `findAllByRole("listitem")` would also match the application shell's
 * navigation, so the list carries an accessible name and every query goes
 * through it.
 */
async function devices(): Promise<HTMLElement[]> {
  const list = await screen.findByRole("list", { name: /sessions/i });
  return within(list).getAllByRole("listitem");
}

/** The nth device row, asserted to exist so the type is not `| undefined`. */
async function deviceAt(index: number): Promise<HTMLElement> {
  const row = (await devices())[index];
  expect(row).toBeDefined();
  return row as HTMLElement;
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  revoked = [];
  listReads = 0;
  signedIn();
});

it("marks the current device and offers no way to sign it out", async () => {
  serveDevices([LAPTOP, PHONE]);
  renderApp({ path: "/settings/sessions" });

  const rows = await devices();
  expect(rows).toHaveLength(2);

  const laptop = await deviceAt(0);
  const phone = await deviceAt(1);
  expect(within(laptop).getByText("Chrome on macOS")).toBeInTheDocument();
  expect(within(laptop).getByText(/current device/i)).toBeInTheDocument();
  // The row for this browser has no per-device control. "Sign out
  // everywhere", below the list, is what ends this session.
  expect(within(laptop).queryByRole("button")).not.toBeInTheDocument();

  expect(within(phone).getByText("Safari on iPhone")).toBeInTheDocument();
  expect(within(phone).getByRole("button", { name: /sign out/i })).toBeInTheDocument();
});

it("says a client is unknown rather than guessing at one", async () => {
  // The server answers `null` for a user agent it does not recognise, and
  // the screen has to render that as an honest gap. A label invented here
  // would be a device list that names something the player never used.
  serveDevices([
    { ...PHONE, browser: null, platform: "Windows" },
    { ...LAPTOP, id: "019fb9ea-0a0c-7cec-9c5f-402727c31a03", browser: null, platform: null },
  ]);
  renderApp({ path: "/settings/sessions" });

  expect(await screen.findByText("Unknown browser on Windows")).toBeInTheDocument();
  expect(screen.getByText("Unknown device")).toBeInTheDocument();
});

it("signs out another device and re-reads the list from the server", async () => {
  mswServer.use(
    http.delete(url("/auth/browser/sessions/:id"), ({ params }) => {
      revoked.push(String(params.id));
      return new HttpResponse(null, { status: 204 });
    }),
  );
  // The second read is what the server says afterwards. The row must
  // disappear because of *this*, not because the client removed it.
  serveDevices([LAPTOP, PHONE], [LAPTOP]);
  renderApp({ path: "/settings/sessions" });

  const phone = await deviceAt(1);
  await userEvent.click(within(phone).getByRole("button", { name: /sign out/i }));

  // Destructive and remote: it cannot be undone from the device it ends,
  // so it asks first — the same weight this page already gives signing out
  // everywhere.
  const dialog = await screen.findByRole("dialog");
  await userEvent.click(within(dialog).getByRole("button", { name: /^sign out$/i }));

  await waitFor(() => expect(revoked).toEqual([PHONE.id]));
  await waitFor(async () => expect(await devices()).toHaveLength(1));
  expect(listReads).toBeGreaterThan(1);
  expect(screen.queryByText("Safari on iPhone")).not.toBeInTheDocument();
});

it("keeps the row and explains itself when the sign-out fails", async () => {
  mswServer.use(
    http.delete(url("/auth/browser/sessions/:id"), () =>
      HttpResponse.json({ code: "not_found", message: "No such device." }, { status: 404 }),
    ),
  );
  serveDevices([LAPTOP, PHONE]);
  renderApp({ path: "/settings/sessions" });

  const phone = await deviceAt(1);
  await userEvent.click(within(phone).getByRole("button", { name: /sign out/i }));
  const dialog = await screen.findByRole("dialog");
  await userEvent.click(within(dialog).getByRole("button", { name: /^sign out$/i }));

  // The dialog stays open with the reason in it. Closing it would leave
  // the row in place with no explanation, which reads as the button doing
  // nothing rather than as the request having failed.
  expect(await within(dialog).findByRole("alert")).toBeInTheDocument();
  expect(screen.getByText("Safari on iPhone")).toBeInTheDocument();
});

it("offers a retry instead of an empty list when the read fails", async () => {
  mswServer.use(
    http.get(url("/auth/browser/sessions"), () =>
      HttpResponse.json({ code: "internal_error", message: "no" }, { status: 500 }),
    ),
  );
  renderApp({ path: "/settings/sessions" });

  // The failure that matters most: a list that renders empty on an error
  // tells a player they have no other devices, which is the opposite of
  // what happened.
  expect(await screen.findByText(/could not be loaded/i)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
});
