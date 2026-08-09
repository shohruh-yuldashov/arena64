import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, expect, it, vi } from "vitest";

import { App } from "@/app/App";
import { createAdminRouter } from "@/app/router";
import { accessToken } from "@/app/session-store";

/**
 * Moderation in the console — A64-024.6 §29.
 *
 * Five tests through the real router. Each asserts something an operator
 * could not detect by looking: that a destructive action cannot happen
 * without confirming it, that the reason travels as the server's
 * identifier rather than as the localised label, that a refusal keeps the
 * dialog and its contents, and that the page reflects what the server
 * actually returned rather than what the click assumed.
 */

const ADMIN = { id: "a", username: "op", display_name: "Op", roles: ["admin"] };

const user = {
  id: "u-1",
  username: "target",
  display_name: null,
  email: "target@example.com",
  is_active: true,
  is_verified: true,
  created_at: "2026-01-01T00:00:00Z",
  is_admin: false,
  admin_role_granted_at: null,
  moderation: { is_restricted: false, restriction: null },
};

const sanction = (overrides: Record<string, unknown> = {}) => ({
  id: "s-1",
  player_id: "u-1",
  username: "target",
  kind: "suspended",
  is_effective: true,
  starts_at: "2026-08-09T12:00:00Z",
  expires_at: null,
  lifted_at: null,
  lifted_by: null,
  case: {
    id: "c-1",
    category: "abuse",
    decision: "suspended",
    reasoning: "Repeated abuse after a warning.",
    opened_by: "a",
    opened_by_username: "op",
    opened_at: "2026-08-09T12:00:00Z",
  },
  ...overrides,
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

interface Stub {
  posts: { url: string; body: unknown }[];
  restrictStatus: number;
  restrictResponse: unknown;
  detail: Record<string, unknown>;
  restrictions: unknown[];
}

function stubApi(overrides: Partial<Stub> = {}): Stub {
  const stub: Stub = {
    posts: [],
    restrictStatus: 201,
    restrictResponse: sanction(),
    detail: user,
    restrictions: [sanction()],
    ...overrides,
  };

  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/auth/browser/refresh")) {
      return Promise.resolve(json({ data: { access_token: "t" } }));
    }
    if (url.endsWith("/admin/me")) return Promise.resolve(json({ data: ADMIN }));

    if (init?.method === "POST") {
      stub.posts.push({ url, body: JSON.parse(String(init.body ?? "{}")) });
      if (url.endsWith("/restore")) {
        return Promise.resolve(
          json({ data: sanction({ is_effective: false, lifted_by: "a" }) }),
        );
      }
      return Promise.resolve(json({ data: stub.restrictResponse }, stub.restrictStatus));
    }

    if (url.includes("/admin/moderation")) {
      return Promise.resolve(json({ data: { items: stub.restrictions, next_cursor: null } }));
    }
    if (url.includes("/admin/users/")) return Promise.resolve(json({ data: stub.detail }));
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
  // jsdom implements `<dialog>` but not its modal methods.
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

it("lists who is restricted, with the reason localised and the status as text", async () => {
  stubApi();
  renderAt("/moderation");

  const table = await screen.findByRole("table");
  expect(within(table).getByRole("link", { name: "target" })).toHaveAttribute(
    "href",
    "/users/u-1",
  );
  // The server sent `abuse`; the console shows the operator's language, and
  // the status is a word rather than a colour.
  expect(within(table).queryByText("abuse")).not.toBeInTheDocument();
  expect(
    within(table).getAllByText(/Suiiste'mol|Злоупотребление|Abuse/).length,
  ).toBeGreaterThan(0);
  expect(within(table).getAllByText(/Amalda|Действует|In force/).length).toBeGreaterThan(0);
});

it("never restricts an account without an explicit confirmation", async () => {
  // The click that matters opens a dialog. Nothing reaches the server until
  // the operator has read the target's name and the consequence and
  // confirmed — a one-click restriction is the accident §21 exists to stop.
  const stub = stubApi();
  const person = userEvent.setup();
  renderAt("/users/u-1");

  await person.click(
    await screen.findByRole("button", { name: /Hisobni cheklash|Ограничить|Restrict/ }),
  );
  expect(stub.posts).toHaveLength(0);

  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/target/)).toBeInTheDocument();

  await person.type(within(dialog).getByRole("textbox"), "Cheating confirmed.");
  await person.click(
    within(dialog).getByRole("button", { name: /Hisobni cheklash|Ограничить|Restrict/ }),
  );

  await waitFor(() => expect(stub.posts).toHaveLength(1));
});

it("sends the reason as the server's identifier and no actor at all", async () => {
  // §12 — the actor is the session's. A payload that named one would be a
  // payload that could name somebody else.
  const stub = stubApi();
  const person = userEvent.setup();
  renderAt("/users/u-1");

  await person.click(
    await screen.findByRole("button", { name: /Hisobni cheklash|Ограничить|Restrict/ }),
  );
  const dialog = await screen.findByRole("dialog");

  await person.selectOptions(within(dialog).getByLabelText(/Sabab|Причина|Reason/), "cheating");
  await person.selectOptions(within(dialog).getByLabelText(/Muddat|Срок|Duration/), "168");
  await person.type(within(dialog).getByRole("textbox"), "Engine assistance.");
  await person.click(
    within(dialog).getByRole("button", { name: /Hisobni cheklash|Ограничить|Restrict/ }),
  );

  await waitFor(() => expect(stub.posts).toHaveLength(1));
  expect(stub.posts[0]?.url).toContain("/admin/users/u-1/restrict");
  expect(stub.posts[0]?.body).toEqual({
    category: "cheating",
    reasoning: "Engine assistance.",
    duration_hours: 168,
  });
});

it("keeps the dialog and what was typed when the server refuses", async () => {
  // A `409` is the platform's safety rules answering, not a network fault.
  // Closing the dialog would make the operator retype a decision they
  // already made — and the second attempt is the one they get wrong.
  const stub = stubApi({ restrictStatus: 409, restrictResponse: { detail: "Already." } });
  const person = userEvent.setup();
  renderAt("/users/u-1");

  await person.click(
    await screen.findByRole("button", { name: /Hisobni cheklash|Ограничить|Restrict/ }),
  );
  const dialog = await screen.findByRole("dialog");
  await person.type(within(dialog).getByRole("textbox"), "Repeat offence.");
  await person.click(
    within(dialog).getByRole("button", { name: /Hisobni cheklash|Ограничить|Restrict/ }),
  );

  await within(dialog).findByRole("alert");
  expect(stub.posts).toHaveLength(1);
  expect(within(dialog).getByRole("textbox")).toHaveValue("Repeat offence.");
});

it("shows the restriction the server returned, then clears it on restore", async () => {
  // The response *is* the new state — it carries `is_effective` computed on
  // the server's clock. Guessing locally would show "restricted" for an
  // action the server had refused, or the wrong expiry for a skewed device.
  const stub = stubApi({
    detail: { ...user, moderation: { is_restricted: true, restriction: sanction() } },
  });
  const person = userEvent.setup();
  renderAt("/users/u-1");

  expect(await screen.findByText(/Cheklangan|Ограничен|Restricted/)).toBeInTheDocument();
  expect(screen.getByText("Repeated abuse after a warning.")).toBeInTheDocument();

  await person.click(screen.getByRole("button", { name: /olib tashlash|Снять|Restore/ }));
  const dialog = await screen.findByRole("dialog");
  await person.click(
    within(dialog).getByRole("button", { name: /olib tashlash|Снять|Restore/ }),
  );

  await waitFor(() => expect(stub.posts).toHaveLength(1));
  expect(stub.posts[0]?.url).toContain("/admin/users/u-1/restore");
  await screen.findByText(/Cheklanmagan|Не ограничен|Not restricted/);
});
