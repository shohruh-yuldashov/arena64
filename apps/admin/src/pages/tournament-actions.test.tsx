import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, expect, it, vi } from "vitest";

import { App } from "@/app/App";
import { createAdminRouter } from "@/app/router";
import { accessToken } from "@/app/session-store";

/**
 * Tournament administration in the console — A64-024.5H §23.
 *
 * Five tests through the real router. Each asserts something an operator
 * could not detect by looking: that the offered action follows the server's
 * state rather than a guess, that unsupported operations have no control at
 * all, that starting confirms, and that a refusal does not leave the page
 * claiming a transition that did not happen.
 */

const ADMIN = { id: "a", username: "op", display_name: "Op", roles: ["admin"] };

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

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

interface Stub {
  posts: { url: string; body: string | null }[];
  status: string;
  commandStatus: number;
}

function stubApi(overrides: Partial<Stub> = {}): Stub {
  const stub: Stub = { posts: [], status: "draft", commandStatus: 200, ...overrides };

  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    if (url.endsWith("/admin/me")) return Promise.resolve(json({ data: ADMIN }));

    if (init?.method === "POST") {
      stub.posts.push({ url, body: (init.body as string | undefined) ?? null });
      if (stub.commandStatus !== 200 && stub.commandStatus !== 201) {
        return Promise.resolve(json({ detail: "no" }, stub.commandStatus));
      }
      // The server's answer is the new state; the page re-reads from it.
      stub.status = url.endsWith("/registration/open")
        ? "registration_open"
        : url.endsWith("/registration/close")
          ? "registration_closed"
          : url.endsWith("/start")
            ? "in_progress"
            : stub.status;
      return Promise.resolve(
        json({ data: { tournament_id: "t-1", status: stub.status, matches_launched: 4 } }),
      );
    }

    if (url.includes("/admin/tournaments/")) {
      return Promise.resolve(json({ data: detail(stub.status) }));
    }
    if (url.includes("/admin/tournaments")) {
      return Promise.resolve(json({ data: { items: [], next_cursor: null } }));
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

it("offers only the command the current state allows", async () => {
  // The lifecycle table is the aggregate's. The console renders from the
  // server's status so an operator is not offered a move that would be
  // refused — but the refusal is still the aggregate's job, not this map's.
  stubApi({ status: "draft" });
  renderAt("/tournaments/t-1");

  await screen.findByRole("heading", { level: 2, name: "Friday Blitz" });
  expect(
    screen.getByRole("button", {
      name: /Ro'yxatni ochish|Открыть регистрацию|Open registration/,
    }),
  ).toBeInTheDocument();
  // A draft cannot be closed or started.
  expect(
    screen.queryByRole("button", { name: /yopish|Закрыть|Close registration/ }),
  ).toBeNull();
  expect(screen.queryByRole("button", { name: /boshlash|Начать|Start tournament/ })).toBeNull();
});

it("never offers cancel, publish-round or entrant removal", async () => {
  // Round publication follows from match results and cancellation has no
  // finished semantics — a greyed-out button for either would imply the
  // platform has an answer it is withholding.
  stubApi({ status: "in_progress" });
  renderAt("/tournaments/t-1");

  await screen.findByRole("heading", { level: 2, name: "Friday Blitz" });
  for (const absent of [/Bekor|Отмен|Cancel/, /Raund|Раунд|Publish/, /Chiqar|Удалить|Remove/]) {
    expect(screen.queryByRole("button", { name: absent })).toBeNull();
  }
  // And the section says so rather than being empty.
  expect(
    screen.getByText(/Bu holat uchun amal yo'q|действий нет|No action is available/),
  ).toBeInTheDocument();
});

it("sends a bodyless command and re-reads the tournament afterwards", async () => {
  // The transition is the route: there is nothing to send, and nothing a
  // caller could send that names a state. After it, the detail is re-read
  // from the server rather than patched locally, so the next set of actions
  // comes from the same authority as the first.
  const stub = stubApi({ status: "draft" });
  const person = userEvent.setup();
  renderAt("/tournaments/t-1");

  await person.click(
    await screen.findByRole("button", {
      name: /Ro'yxatni ochish|Открыть регистрацию|Open registration/,
    }),
  );

  await waitFor(() => expect(stub.posts).toHaveLength(1));
  expect(stub.posts[0]?.url).toContain("/admin/tournaments/t-1/registration/open");
  expect(stub.posts[0]?.body).toBe("{}");

  // The re-read now offers the *next* command, not the one just used.
  await screen.findByRole("button", { name: /yopish|Закрыть|Close registration/ });
});

it("requires confirmation before starting, and names the tournament", async () => {
  // Starting freezes the field, builds the bracket and creates real games
  // for real people. §18's bar for a deliberate confirmation.
  const stub = stubApi({ status: "registration_closed" });
  const person = userEvent.setup();
  renderAt("/tournaments/t-1");

  await person.click(
    await screen.findByRole("button", { name: /boshlash|Начать турнир|Start tournament/ }),
  );
  expect(stub.posts).toHaveLength(0);

  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/Friday Blitz/)).toBeInTheDocument();
  await person.click(
    within(dialog).getByRole("button", { name: /boshlash|Начать турнир|Start tournament/ }),
  );

  await waitFor(() => expect(stub.posts).toHaveLength(1));
  expect(stub.posts[0]?.url).toContain("/admin/tournaments/t-1/start");
});

it("keeps the page honest when the server refuses a transition", async () => {
  // A 409 is the aggregate saying the tournament is not in that state. The
  // console must not show it as moved.
  const stub = stubApi({ status: "draft", commandStatus: 409 });
  const person = userEvent.setup();
  renderAt("/tournaments/t-1");

  await person.click(
    await screen.findByRole("button", {
      name: /Ro'yxatni ochish|Открыть регистрацию|Open registration/,
    }),
  );

  await screen.findByRole("alert");
  expect(stub.posts).toHaveLength(1);
  // Still a draft: the only action offered is the one that was refused.
  expect(
    screen.queryByRole("button", { name: /yopish|Закрыть|Close registration/ }),
  ).toBeNull();
});
