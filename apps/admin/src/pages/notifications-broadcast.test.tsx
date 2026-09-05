import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, expect, it, vi } from "vitest";

import { App } from "@/app/App";
import { createAdminRouter } from "@/app/router";
import { accessToken } from "@/app/session-store";

/**
 * The broadcast composer and history — A64-027A §38, §39.
 *
 * The highest-reach control in the console: one submission writes a row
 * into every eligible inbox, and there is no unsend. So the tests here are
 * about the things that would be invisible until the moment they mattered —
 * that nothing is sent without a confirmation, that a double click cannot
 * send twice, that a recipient count is never invented, and that an
 * uncounted audience is not reported as zero.
 */

const ADMIN = { id: "a", username: "op", display_name: "Operator", roles: ["admin"] };

const broadcast = (overrides: Record<string, unknown> = {}) => ({
  id: "b-1",
  title: "Rejalashtirilgan texnik ishlar",
  body: "Bugun soat 23:00 dan 23:30 gacha platforma ishlamaydi.",
  locale: "uz",
  audience: "all_players",
  channel: "in_app",
  status: "queued",
  created_at: "2026-09-05T08:00:00Z",
  started_at: null,
  completed_at: null,
  audience_size: null,
  delivered: 0,
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
  posts: { url: string; body: Record<string, unknown> }[];
  gets: string[];
  sendStatus: number;
  size: number | null;
  history: unknown[];
}

function stubApi(overrides: Partial<Stub> = {}): Stub {
  const stub: Stub = {
    posts: [],
    gets: [],
    sendStatus: 202,
    size: 12548,
    history: [],
    ...overrides,
  };

  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    if (url.endsWith("/admin/me")) return Promise.resolve(json({ data: ADMIN }));

    if (init?.method === "POST" && url.includes("/admin/broadcasts")) {
      const body = JSON.parse((init.body as string) || "{}") as Record<string, unknown>;
      stub.posts.push({ url, body });
      return Promise.resolve(json({ data: broadcast({ title: body.title }) }, stub.sendStatus));
    }

    if (url.includes("/admin/broadcasts/audience/")) {
      stub.gets.push(url);
      if (stub.size === null) return Promise.resolve(json({}, 503));
      return Promise.resolve(json({ data: { audience: "all_players", size: stub.size } }));
    }
    if (url.includes("/admin/broadcasts")) {
      stub.gets.push(url);
      return Promise.resolve(json({ data: { items: stub.history } }));
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

async function compose(person: ReturnType<typeof userEvent.setup>) {
  await person.type(
    await screen.findByLabelText(/Sarlavha|Заголовок|^Title$/),
    "Texnik ishlar",
  );
  await person.type(
    screen.getByLabelText(/^Matn$|Сообщение|^Message$/),
    "Bugun kechqurun 30 daqiqa.",
  );
}

it("shows the server's recipient count and never one of its own", async () => {
  const stub = stubApi({ size: 12548 });
  renderAt("/notifications?tab=send");

  expect((await screen.findAllByText(/12,548/))[0]).toBeInTheDocument();
  expect(stub.gets.some((url) => url.includes("/audience/all_players"))).toBe(true);
});

it("says the count is unknown rather than showing a plausible number", async () => {
  // A fallback figure here is the most trusted wrong number in the console:
  // it is read immediately before deciding to address everybody.
  stubApi({ size: null });
  renderAt("/notifications?tab=send");

  expect(
    await screen.findByText(
      /olishning iloji bo'lmadi|не удалось получить|could not be fetched/i,
    ),
  ).toBeInTheDocument();
  expect(screen.queryByText(/\d{1,3}(,\d{3})+/)).not.toBeInTheDocument();
});

it("sends nothing until the confirmation is accepted", async () => {
  const stub = stubApi();
  const person = userEvent.setup();
  renderAt("/notifications?tab=send");

  await compose(person);
  await person.click(screen.getByRole("button", { name: /Ko'rib chiqish|Проверить|Review/ }));

  // The dialog is open and still nothing has been sent.
  const dialog = await screen.findByRole("dialog");
  expect(stub.posts).toHaveLength(0);

  // And it restates what the send will do, so the operator confirms the
  // action rather than the click.
  expect(within(dialog).getByText("Texnik ishlar")).toBeInTheDocument();
  expect(within(dialog).getByText(/12,548/)).toBeInTheDocument();

  await person.click(within(dialog).getByRole("button", { name: /Yuborish|Отправить|^Send$/ }));
  await waitFor(() => {
    expect(stub.posts).toHaveLength(1);
  });
});

it("warns explicitly when the audience is the whole platform", async () => {
  stubApi();
  const person = userEvent.setup();
  renderAt("/notifications?tab=send");

  await compose(person);
  await person.click(screen.getByRole("button", { name: /Ko'rib chiqish|Проверить|Review/ }));

  const dialog = await screen.findByRole("dialog");
  expect(
    within(dialog).getByText(/butun platformaga|всей платформе|whole platform/i),
  ).toBeInTheDocument();
});

it("carries one idempotency key, so a repeated send cannot become two", async () => {
  // §18. The key is minted per composition, not per submit — a double
  // click, a slow network and an impatient retry all carry the same one and
  // the server returns the broadcast it already made.
  const stub = stubApi({ sendStatus: 503 });
  const person = userEvent.setup();
  renderAt("/notifications?tab=send");

  await compose(person);
  await person.click(screen.getByRole("button", { name: /Ko'rib chiqish|Проверить|Review/ }));
  const dialog = await screen.findByRole("dialog");
  const send = within(dialog).getByRole("button", { name: /Yuborish|Отправить|^Send$/ });

  await person.click(send);
  await waitFor(() => {
    expect(stub.posts).toHaveLength(1);
  });
  // The failure keeps the dialog open, and the retry reuses the key.
  await person.click(
    within(await screen.findByRole("dialog")).getByRole("button", {
      name: /Yuborish|Отправить|^Send$/,
    }),
  );
  await waitFor(() => {
    expect(stub.posts).toHaveLength(2);
  });

  expect(stub.posts[0]?.body.idempotency_key).toBe(stub.posts[1]?.body.idempotency_key);
});

it("keeps the composed text when a send fails", async () => {
  stubApi({ sendStatus: 503 });
  const person = userEvent.setup();
  renderAt("/notifications?tab=send");

  await compose(person);
  await person.click(screen.getByRole("button", { name: /Ko'rib chiqish|Проверить|Review/ }));
  await person.click(
    within(await screen.findByRole("dialog")).getByRole("button", {
      name: /Yuborish|Отправить|^Send$/,
    }),
  );

  expect(await screen.findByRole("alert")).toBeInTheDocument();
  expect(screen.getByLabelText(/Sarlavha|Заголовок|^Title$/)).toHaveValue("Texnik ishlar");
});

it("sends no link, image or markup field", async () => {
  // The open-redirect protection, from the client's side: the request body
  // is exactly the four fields the domain accepts.
  const stub = stubApi();
  const person = userEvent.setup();
  renderAt("/notifications?tab=send");

  await compose(person);
  await person.click(screen.getByRole("button", { name: /Ko'rib chiqish|Проверить|Review/ }));
  await person.click(
    within(await screen.findByRole("dialog")).getByRole("button", {
      name: /Yuborish|Отправить|^Send$/,
    }),
  );
  await waitFor(() => {
    expect(stub.posts).toHaveLength(1);
  });

  expect(Object.keys(stub.posts[0]?.body ?? {}).sort()).toEqual([
    "audience",
    "body",
    "idempotency_key",
    "locale",
    "recipients",
    "title",
  ]);
});

it("refuses a named audience that is not a list of ids", async () => {
  stubApi();
  const person = userEvent.setup();
  renderAt("/notifications?tab=send");

  await person.click(
    await screen.findByRole("button", { name: /Tanlangan|Выбранные|Specific users/ }),
  );
  await person.type(screen.getByLabelText(/ID lari|ID пользователей|User IDs/), "not-an-id");

  expect(
    await screen.findByText(/ID bo'lmagan|не являющееся ID|not an ID/i),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /Ko'rib chiqish|Проверить|Review/ }),
  ).toBeDisabled();
});

it("reports an uncounted audience as a dash and not as zero", async () => {
  // `audience_size` is `null` until the worker has counted. Rendering it as
  // `0` would show a broadcast that reached nobody.
  stubApi({
    history: [
      broadcast({ id: "b-1", status: "queued", audience_size: null, delivered: 0 }),
      broadcast({ id: "b-2", status: "completed", audience_size: 12548, delivered: 11982 }),
    ],
  });
  renderAt("/notifications?tab=history");

  const table = await screen.findByRole("table");
  expect(within(table).getByText(/hisoblanmoqda|подсчёт|counting/i)).toBeInTheDocument();
  expect(within(table).getByText("11,982 / 12,548")).toBeInTheDocument();
});

it("names no recipient in the history", async () => {
  stubApi({
    history: [broadcast({ audience: "specific_players", named_recipients: 3 })],
  });
  renderAt("/notifications?tab=history");

  const table = await screen.findByRole("table");
  // The count travels; the identity never does.
  expect(within(table).getByText(/·\s*3/)).toBeInTheDocument();
  expect(document.body.textContent ?? "").not.toMatch(
    /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/,
  );
});

it("does not fetch the delivery listing while the composer is open", async () => {
  // §34 — a request per visit for a table nobody has opened.
  const stub = stubApi();
  renderAt("/notifications?tab=send");

  await screen.findAllByText(/12,548/);
  expect(stub.gets.some((url) => url.includes("/admin/notifications"))).toBe(false);
});

it("renders no untranslated keys", async () => {
  stubApi();
  renderAt("/notifications?tab=send");

  await screen.findAllByText(/12,548/);
  expect(document.body.textContent ?? "").not.toMatch(/broadcast\.[a-zA-Z]/);
});
