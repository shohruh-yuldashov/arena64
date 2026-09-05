import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, expect, it, vi } from "vitest";

import { App } from "@/app/App";
import { createAdminRouter } from "@/app/router";
import { accessToken } from "@/app/session-store";

/**
 * The completion pass — A64-024 hardening §3, §5, §8.
 *
 * Four tests over behaviour a reviewer cannot see in a diff: that a
 * repeated click sends one command rather than two, that a refusal names
 * which command was refused, that a detail page links into the consoles
 * that own the rest of the story, and that the shared error element keeps
 * the role it exists for.
 */

const ADMIN = { id: "a", username: "op", display_name: "Op", roles: ["admin"] };

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const detail = (status: string) => ({
  tournament: {
    tournament_id: "t-1",
    name: "Friday Blitz",
    format: "single_elimination",
    variant: "russian_8x8",
    speed_class: "blitz",
    status,
    rated: true,
    capacity: 8,
    entrant_count: 4,
    registration_deadline: null,
    started_at: null,
    completed_at: null,
    created_at: "2026-08-09T12:00:00Z",
  },
  entrants: [],
  rounds: [],
  pairings: [],
  standings: [],
});

const account = {
  id: "u-1",
  username: "player",
  display_name: null,
  email: "player@example.com",
  is_active: true,
  is_verified: true,
  created_at: "2026-08-09T12:00:00Z",
  is_admin: false,
  admin_role_granted_at: null,
  moderation: { is_restricted: false, restriction: null },
};

interface Stub {
  posts: string[];
  commandStatus: number;
  /** Held open so a second click lands while the first is in flight. */
  release?: () => void;
}

function stubApi(overrides: Partial<Stub> = {}): Stub {
  const stub: Stub = { posts: [], commandStatus: 200, ...overrides };

  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    if (url.endsWith("/admin/me")) return Promise.resolve(json({ data: ADMIN }));

    if (init?.method === "POST") {
      stub.posts.push(url);
      const answer = json(
        stub.commandStatus === 200
          ? { data: { tournament_id: "t-1", status: "registration_open", matches_launched: 0 } }
          : { detail: "no" },
        stub.commandStatus,
      );
      if (stub.release === undefined) return Promise.resolve(answer);
      return new Promise<Response>((resolve) => {
        stub.release = () => resolve(answer);
      });
    }

    if (url.includes("/admin/users/")) return Promise.resolve(json({ data: account }));
    if (url.includes("/admin/tournaments/"))
      return Promise.resolve(json({ data: detail("draft") }));
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

it("sends one command however many times the button is clicked", async () => {
  // The aggregate's row lock makes a duplicate harmless to the tournament,
  // and would still write two audit entries for one action an operator
  // took once. The disabled attribute is UX; the guard is in the handler.
  const stub = stubApi({ release: () => undefined });
  const person = userEvent.setup();
  renderAt("/tournaments/t-1");

  const open = await screen.findByRole("button", {
    name: /Ro'yxatni ochish|Открыть регистрацию|Open registration/,
  });
  await person.click(open);
  await person.click(open);
  await person.click(open);

  expect(stub.posts).toHaveLength(1);
});

it("says which command was refused, not merely that something was", async () => {
  // The server answers `409` and nothing more — the reason is a domain
  // state rather than a message. Which of the three buttons was pressed is
  // the only thing that distinguishes them, and it is on this side.
  stubApi({ commandStatus: 409 });
  const person = userEvent.setup();
  renderAt("/tournaments/t-1");

  await person.click(
    await screen.findByRole("button", {
      name: /Ro'yxatni ochish|Открыть регистрацию|Open registration/,
    }),
  );

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(
    /Ro'yxatni ochib bo'lmadi|открыть регистрацию|Registration could not be opened/,
  );
});

it("links an account into the consoles that own the rest of its story", async () => {
  // §7 — and every parameter is one the destination's own `validateSearch`
  // declares. A filter the destination ignores is one an operator believes
  // is applied.
  stubApi();
  renderAt("/users/u-1");

  await screen.findByRole("heading", { level: 2, name: "player" });
  const links = screen.getByText(/o'yinlari|Партии этого|Matches for this/).closest("p");
  expect(links).not.toBeNull();

  expect(
    within(links as HTMLElement).getByRole("link", {
      name: /o'yinlari|Партии этого|Matches for this/,
    }),
  ).toHaveAttribute("href", "/matches?participant=u-1");
  expect(
    within(links as HTMLElement).getByRole("link", {
      name: /Barcha cheklovlar|Все ограничения|All restrictions/,
    }),
  ).toHaveAttribute("href", "/moderation");
});

it("announces a load failure through the shared error element", async () => {
  // Thirteen copies of this element agreed until one of them stopped. The
  // property worth keeping is the `role` — an error that renders silently
  // is one the operator does not know happened.
  //
  // A64-027A replaced the bare notice on this page with `ErrorState`, which
  // also offers a retry. The class is no longer the thing being asserted;
  // the announcement and the absence of a half-rendered table are.
  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    if (url.endsWith("/admin/me")) return Promise.resolve(json({ data: ADMIN }));
    return Promise.resolve(json({}, 500));
  });
  renderAt("/users");

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/\S/);
  await waitFor(() => expect(screen.queryByRole("table")).toBeNull());
});
