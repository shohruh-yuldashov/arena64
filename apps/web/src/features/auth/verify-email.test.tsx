import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";

import { env } from "@/shared/config/env";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp } from "@/shared/test/render";

/**
 * The six-digit verification screen — A64-021.5H §31.11.
 *
 * Through the **real router**, so route registration, the search
 * validators, both guards and the session provider are exercised together.
 * §33's rule from earlier phases applies unchanged: a component rendered in
 * isolation is not a reachability proof, and what is substituted here is the
 * HTTP layer alone.
 *
 * Three claims, and each is a different way the flow can be wrong:
 *
 *   an **unverified** session on a product route is sent to `/verify-email`
 *     rather than shown a screen whose every button answers `403`
 *
 *   a code is typed, submitted, and the session becomes verified from the
 *     **server's** answer — not from anything this client decided
 *
 *   a rejected code says which rejection it was, because "wrong" and
 *     "expired" are different instructions to the person
 */

const url = (path: string) => `${env.VITE_API_URL}${path}`;
const envelope = <T,>(data: T) => ({ data, meta: { request_id: null, correlation_id: null } });

const UNVERIFIED = {
  id: "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
  username: "newcomer",
  display_name: null,
  email: "newcomer@example.com",
  is_active: true,
  is_verified: false,
};

const VERIFIED = { ...UNVERIFIED, is_verified: true };

function signedIn(user: Record<string, unknown>): void {
  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json(envelope({ access_token: "token-1", user })),
    ),
  );
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
});

it("sends an unverified session from a product route to the code screen", async () => {
  // The guard, and the thing it prevents: a page whose every write is
  // refused server-side rendering as though it worked.
  signedIn(UNVERIFIED);
  renderApp({ path: "/friends" });

  expect(await screen.findByRole("heading", { name: /enter your code/i })).toBeVisible();
  // Masked, not printed — §21. Enough to recognise which address it went
  // to, and not enough to publish on a shared screen.
  // The domain survives so somebody can tell which mailbox to open; the
  // local part does not.
  expect(screen.getByText(/n•+@example\.com/)).toBeVisible();
  expect(screen.queryByText(/newcomer@example\.com/)).not.toBeInTheDocument();
});

it("verifies with the typed code and applies the server's answer", async () => {
  let submitted: unknown = null;
  signedIn(UNVERIFIED);
  mswServer.use(
    http.post(url("/auth/email/verify-code"), async ({ request }) => {
      submitted = await request.json();
      return HttpResponse.json(envelope(VERIFIED));
    }),
  );
  renderApp({ path: "/verify-email" });

  const field = await screen.findByLabelText(/verification code/i);
  // The three attributes that make this usable on a phone — §20. Asserted
  // because `autocomplete="one-time-code"` is the only way iOS offers the
  // code from the message, and losing it is invisible on a desktop.
  expect(field).toHaveAttribute("inputmode", "numeric");
  expect(field).toHaveAttribute("autocomplete", "one-time-code");
  expect(field).toHaveAttribute("maxlength", "6");

  // Pasted rather than typed, which is what people actually do — and the
  // path that a six-input implementation gets wrong.
  await userEvent.click(field);
  await userEvent.paste("482193");

  await userEvent.click(screen.getByRole("button", { name: /^verify$/i }));

  await waitFor(() => expect(submitted).toEqual({ code: "482193" }));
  // The session took the server's `UserRead`, so this screen has nothing
  // left to do and navigates away — which is the observable proof that
  // `applyUser` ran rather than a local flag being flipped.
  await waitFor(() =>
    expect(screen.queryByRole("heading", { name: /enter your code/i })).not.toBeInTheDocument(),
  );
});

it("says which rejection it was", async () => {
  signedIn(UNVERIFIED);
  mswServer.use(
    http.post(url("/auth/email/verify-code"), () =>
      HttpResponse.json(
        { code: "email_verification_code_expired", message: "no", request_id: null },
        { status: 422 },
      ),
    ),
  );
  renderApp({ path: "/verify-email" });

  const field = await screen.findByLabelText(/verification code/i);
  await userEvent.click(field);
  await userEvent.paste("000000");
  await userEvent.click(screen.getByRole("button", { name: /^verify$/i }));

  // Not "that code is not correct" — retyping an expired code is
  // pointless, and telling somebody to do it wastes one of five attempts.
  expect(await screen.findByRole("alert")).toHaveTextContent(/expired/i);
});
