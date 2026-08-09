import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http } from "msw";
import { describe, expect, it } from "vitest";

import { FormField } from "@/features/auth/ui/form-field";
import { PasswordField } from "@/features/auth/ui/password-field";
import { env } from "@/shared/config/env";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp, renderWithProviders } from "@/shared/test/render";

/**
 * The authentication UX boundaries — A64-025.4 §19.
 *
 * `auth.test.tsx` already covers the flows: signing in, the bounded
 * credential message, the session states. This covers what A64-025.4 added
 * or moved, and nothing it already proved.
 */

const PASSWORD = /password|parol|пароль/i;

describe("the password field", () => {
  it("reveals and hides without touching the value", async () => {
    const person = userEvent.setup();
    renderWithProviders(<PasswordField label="Password" autoComplete="current-password" />);

    const input = screen.getByLabelText(PASSWORD, { selector: "input" });
    await person.type(input, "CorrectHorse1!");
    expect(input).toHaveAttribute("type", "password");

    const toggle = screen.getByRole("button", { name: /show password/i });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    await person.click(toggle);

    // The same element, still holding the same value — only `type` moved.
    expect(input).toHaveAttribute("type", "text");
    expect(input).toHaveValue("CorrectHorse1!");
    expect(screen.getByRole("button", { name: /hide password/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("never submits the form it sits in", async () => {
    // A bare `<button>` inside a `<form>` submits it. Somebody checking
    // what they typed would post the form instead.
    const person = userEvent.setup();
    let submitted = 0;
    renderWithProviders(
      <form
        onSubmit={(event) => {
          event.preventDefault();
          submitted += 1;
        }}
      >
        <PasswordField label="Password" autoComplete="current-password" />
      </form>,
    );

    await person.click(screen.getByRole("button", { name: /show password/i }));
    expect(submitted).toBe(0);
  });

  it("keeps the caller's autocomplete", () => {
    // `current-password` and `new-password` are a distinction password
    // managers act on; the toggle must not flatten it.
    renderWithProviders(<PasswordField label="New password" autoComplete="new-password" />);
    expect(screen.getByLabelText(PASSWORD, { selector: "input" })).toHaveAttribute(
      "autocomplete",
      "new-password",
    );
  });
});

describe("the form field", () => {
  it("ties the label, the description and the error to one input", () => {
    // Four ids have to agree. Written by hand they eventually do not, and
    // the symptom is invisible to anybody who can see the red border.
    renderWithProviders(
      <FormField
        label="Email"
        description="We never share it"
        error="That is not an address"
      />,
    );

    const input = screen.getByLabelText(/email/i, { selector: "input" });
    expect(input).toHaveAttribute("aria-invalid", "true");

    const describedBy = input.getAttribute("aria-describedby")?.split(" ") ?? [];
    expect(describedBy).toHaveLength(2);
    const described = describedBy.map((id) => document.getElementById(id)?.textContent);
    expect(described).toContain("That is not an address");
    expect(described).toContain("We never share it");
  });

  it("says the error in words, not only in colour", () => {
    renderWithProviders(<FormField label="Email" error="That is not an address" />);
    expect(screen.getByRole("alert")).toHaveTextContent("That is not an address");
  });

  it("adds nothing to describe when the field is valid", () => {
    renderWithProviders(<FormField label="Email" />);
    const input = screen.getByLabelText(/email/i, { selector: "input" });
    expect(input).not.toHaveAttribute("aria-describedby");
    expect(input).toHaveAttribute("aria-invalid", "false");
  });
});

describe("the authentication shell", () => {
  it("gives each page exactly one first-level heading and a way home", async () => {
    renderApp({ path: "/login" });

    await screen.findByRole("heading", { level: 1 });
    // The shell's own header carries one link home; the auth surface adds
    // its own for the same reason every sign-in page does.
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);

    const home = screen
      .getAllByRole("link")
      .filter((link) => link.getAttribute("href") === "/");
    expect(home.length).toBeGreaterThan(0);
  });

  it("keeps the decorative board out of the accessibility tree", async () => {
    const { container } = renderApp({ path: "/login" });
    await screen.findByRole("heading", { level: 1 });

    // The panel is identity, not information: its sentence is real text and
    // the grid behind it must not be announced.
    expect(container.querySelectorAll('[aria-hidden="true"]').length).toBeGreaterThan(0);
  });
});

describe("the sign-in form", () => {
  it("guards against a double submit while one is in flight", async () => {
    // A request that never answers, so "in flight" is a state the test can
    // actually observe. Without it the submit resolves before the assertion
    // and the guard looks absent when it is merely finished.
    mswServer.use(
      http.post(`${env.VITE_API_URL}/auth/browser/login`, () => new Promise(() => {})),
    );

    const person = userEvent.setup();
    renderApp({ path: "/login" });

    const submit = await screen.findByRole("button", { name: /sign in|kirish|войти/i });
    const form = submit.closest("form");
    expect(form).not.toBeNull();

    await person.type(
      within(form as HTMLElement).getByLabelText(/email|pochta|почта/i, { selector: "input" }),
      "player@example.com",
    );
    await person.type(
      within(form as HTMLElement).getByLabelText(PASSWORD, { selector: "input" }),
      "CorrectHorse1!",
    );
    await person.click(submit);

    // `disabled` while submitting is the guard; a second click cannot reach
    // the handler at all.
    await waitFor(() => expect(submit).toBeDisabled());
    await person.click(submit);
    expect(submit).toBeDisabled();
  });
});
