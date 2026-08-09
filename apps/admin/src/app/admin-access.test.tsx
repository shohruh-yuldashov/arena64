import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "@/app/App";

/**
 * The admin console's authorization gate — A64-024.1 §13.
 *
 * Five tests over the **real** `App`, the real gate and the real shell,
 * with `fetch` stubbed at the browser boundary. What is asserted is what a
 * player would see, because the failure this file guards against is
 * privileged chrome reaching somebody who may not have it.
 *
 * The backend is not simulated beyond its status codes: `403` and `401`
 * are what `require_admin` and `CurrentUser` actually produce, and the
 * client's whole job is to branch on them without inventing a third
 * answer.
 */

function stubFetch(responder: (url: string) => Response | Promise<Response>) {
  vi.stubGlobal("fetch", (input: RequestInfo | URL) =>
    Promise.resolve(responder(String(input))),
  );
}

const SESSION = {
  id: "019fd1c7-5178-7a94-8076-4eeece03a8f4",
  username: "operator",
  display_name: "Operator",
  roles: ["admin"],
};

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the authorization gate", () => {
  it("never renders the shell to an authenticated non-administrator", async () => {
    // §13.1. The case the whole app exists to refuse. A `403` is what
    // `require_admin` returns for a signed-in player, and the console must
    // render a refusal rather than chrome — no navigation, no sign-out,
    // nothing that suggests the surface is theirs.
    stubFetch(() => json({ detail: "forbidden" }, 403));

    render(<App />);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.queryByRole("button", { name: /sign out|chiqish|выйти/i })).toBeNull();
  });

  it("renders the shell only for a server-authorized administrator", async () => {
    // §13.2. The one branch that reaches the shell, and it is reached by a
    // `200` from the server rather than by any local state.
    stubFetch((url) => (url.includes("/admin/me") ? json({ data: SESSION }) : json({})));

    render(<App />);

    const navigation = await screen.findByRole("navigation");
    expect(navigation).toBeInTheDocument();
    expect(screen.getByText(/Operator/)).toBeInTheDocument();
  });

  it("shows nothing privileged while authorization is still being checked", async () => {
    // §13.3 — the flash. A gate that starts optimistic paints the shell
    // for one frame before the server answers, which on a slow connection
    // is not one frame. Held here by a `fetch` that never settles: the
    // console must be in its checking state and must have no navigation.
    stubFetch(() => new Promise<Response>(() => {}) as unknown as Response);

    render(<App />);

    expect(await screen.findByRole("status")).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).toBeNull();
  });

  it("asks the server on load, so a direct visit and a refresh behave alike", async () => {
    // §13.4. There is no cached decision and no stored role: mounting the
    // app *is* the check, which is why a bookmark, a refresh and a fresh
    // sign-in all follow the same path. Asserted by the request itself —
    // and by its credentials, without which the session would not travel.
    const calls: string[] = [];
    vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push(String(input));
      expect(init?.credentials).toBe("include");
      return Promise.resolve(json({ data: SESSION }));
    });

    render(<App />);

    await waitFor(() => expect(calls).toContain("/api/v1/admin/me"));
  });

  it("declares unbuilt sections as disabled rather than as broken links", async () => {
    // §13.5. Accessibility of the placeholder navigation: a section that
    // does not exist must be announced as unavailable, not rendered as a
    // link that goes nowhere. `aria-current` marks the live one, so the
    // active section is available to a screen reader and not only to a
    // colour.
    stubFetch(() => json({ data: SESSION }));

    render(<App />);

    const navigation = await screen.findByRole("navigation");
    const sections = within(navigation).getAllByRole("button");

    expect(sections.length).toBeGreaterThan(1);
    expect(sections.filter((button) => button.hasAttribute("disabled")).length).toBe(
      sections.length - 1,
    );
    expect(
      sections.filter((button) => button.getAttribute("aria-current") === "page"),
    ).toHaveLength(1);
  });
});

it("signs out through the server and re-asks rather than assuming", async () => {
  // A sign-out that only cleared local state would leave a live session on
  // the server and a signed-out screen in the browser — the two disagreeing
  // is how a shared machine leaks an admin session.
  const calls: string[] = [];
  let authorized = true;
  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const url = String(input);
    calls.push(url);
    if (url.includes("logout")) {
      authorized = false;
      return Promise.resolve(json({}));
    }
    return Promise.resolve(authorized ? json({ data: SESSION }) : json({}, 401));
  });

  render(<App />);
  const signOut = await screen.findByRole("button", { name: /sign out|chiqish|выйти/i });
  await userEvent.click(signOut);

  await waitFor(() => expect(screen.queryByRole("navigation")).toBeNull());
  expect(calls.filter((url) => url.includes("/admin/me")).length).toBeGreaterThan(1);
});
