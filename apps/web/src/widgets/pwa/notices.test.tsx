import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";

import { env } from "@/shared/config/env";
import {
  INSTALL_DISMISSED_KEY,
  registerServiceWorker,
  watchInstallability,
} from "@/shared/pwa";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * The PWA notices, through the **real application** — A64-020.9 §28.8,
 * §28.9, §23, §32.
 *
 * Both tests mount `App`, so what they assert is that `AppShell` mounts
 * the strip, that the session gates the install offer, and that the whole
 * graph agrees — not that three components render in isolation. §32 is
 * explicit that an isolated test is insufficient, and this is the
 * reachability proof for every notice in the phase.
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

const IPHONE_SAFARI =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1";

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

/**
 * Chromium's offer of an install dialog. Constructed by hand because no
 * environment fires it, and because the shape this application depends on
 * is exactly three members.
 */
function offerInstallPrompt(): void {
  const event = new Event("beforeinstallprompt", { cancelable: true });
  Object.assign(event, {
    prompt: () => Promise.resolve(),
    userChoice: Promise.resolve({ outcome: "accepted" as const }),
  });
  window.dispatchEvent(event);
}

beforeEach(() => {
  localStorage.clear();
  vi.spyOn(console, "error").mockImplementation(() => {});
});

it("offers installation only after sign-in, and remembers both answers", async () => {
  watchInstallability(window);
  offerInstallPrompt();

  // A visitor who has not signed in has nothing to install a shortcut to —
  // §16's rule against prompting on first paint, expressed as the trigger
  // this product actually has.
  signedOut();
  const anonymous = renderApp({ path: "/" });
  expect(await screen.findByRole("heading", { level: 1, name: "Arena64" })).toBeVisible();
  expect(screen.queryByText("Install Arena64")).not.toBeInTheDocument();
  anonymous.unmount();

  signedIn();
  const session = renderApp({ path: "/" });
  expect(await screen.findByText("Install Arena64")).toBeVisible();

  await userEvent.click(screen.getByRole("button", { name: "Later" }));
  await waitFor(() => expect(screen.queryByText("Install Arena64")).not.toBeInTheDocument());
  // Persisted, so the question is not asked again next session — the nag
  // §16 forbids is a dismissal that only lasts until the next page load.
  expect(localStorage.getItem(INSTALL_DISMISSED_KEY)).toBe("1");
  session.unmount();

  // Installed by the browser's own menu rather than by our button. The
  // offer disappears because `appinstalled` says so, not because anything
  // inferred it from a click.
  localStorage.clear();
  watchInstallability(window);
  offerInstallPrompt();
  renderApp({ path: "/" });
  expect(await screen.findByText("Install Arena64")).toBeVisible();

  window.dispatchEvent(new Event("appinstalled"));
  await waitFor(() => expect(screen.queryByText("Install Arena64")).not.toBeInTheDocument());
});

it("announces offline and update status, and guides an iOS install, accessibly", async () => {
  // No `beforeinstallprompt` on this platform — there is nothing to defer
  // and nothing to trigger, so the offer is words rather than a button.
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue(IPHONE_SAFARI);
  vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
  watchInstallability(window);

  // A worker waiting to replace the one running this page.
  const waiting = new EventTarget();
  Object.assign(waiting, { postMessage: () => undefined });
  const registration = Object.assign(new EventTarget(), {
    waiting,
    installing: null,
    update: () => Promise.resolve(),
  });
  const container = Object.assign(new EventTarget(), {
    controller: {},
    register: () => Promise.resolve(registration),
  });
  await registerServiceWorker({
    enabled: true,
    container: container as unknown as ServiceWorkerContainer,
    reload: () => undefined,
  });

  signedIn();
  renderApp({ path: "/" });
  expect(await screen.findByRole("heading", { level: 1, name: "Arena64" })).toBeVisible();

  // §23: both are changes the user did not cause, so both are announced —
  // politely, in live regions, rather than by stealing focus.
  const announcements = await screen.findAllByRole("status");
  const announced = announcements.map((region) => region.textContent ?? "").join(" ");
  expect(announced).toContain("No internet connection");
  expect(announced).toContain("A new version is ready");

  // §23: every control has an accessible name and none relies on colour.
  const update = screen.getByRole("button", { name: "Update" });
  expect(screen.getByRole("button", { name: "Retry connection" })).toBeVisible();
  expect(screen.getAllByRole("button", { name: "Later" }).length).toBeGreaterThan(0);

  // §15, §23: not a modal. Nothing took focus on mount, and the update
  // control is reachable by keyboard — which is what lets the prompt sit
  // visible through a whole game without trapping anybody in it.
  expect(document.body).toHaveFocus();
  update.focus();
  expect(update).toHaveFocus();

  // §17: iOS is told how to install, and never told that we installed.
  expect(screen.getByText("Add to Home Screen")).toBeVisible();
  expect(screen.getByText(/Tap Share in Safari/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "Install" })).not.toBeInTheDocument();
});
