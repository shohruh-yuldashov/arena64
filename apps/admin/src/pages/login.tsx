import { useNavigate, useSearch } from "@tanstack/react-router";
import { type FormEvent, useId, useRef, useState } from "react";

import { accessToken } from "@/app/session-store";
import { safeRedirect } from "@/app/safe-redirect";
import { signIn } from "@/shared/api/client";
import { type TranslationKey, useTranslation } from "@/shared/i18n";

/**
 * The admin console's sign-in — A64-024.2 §2, §3, §15.
 *
 * ## Ordinary Arena64 credentials, and the console's own session
 *
 * There is no admin username, no admin password and no admin registration.
 * An administrator is a normal account that holds a live role, so this
 * posts to the **same** `/auth/browser/login` the player client uses. What
 * differs is the origin: this app has its own, so the host-only refresh
 * cookie it receives is the console's and never `apps/web`'s.
 *
 * ## Signing in is not being authorized
 *
 * A successful login stores an access token and **navigates**; it renders
 * nothing privileged. Whether the account may administer anything is
 * `/admin/me`'s answer, asked by the route guard after this page is gone.
 * That is §3's rule — a valid non-administrator gets a session and then a
 * refusal, and this page cannot short-circuit it because it never sees the
 * role.
 *
 * ## Accessibility — §15
 *
 * Real `<label for>` pairs, a native `<form>` so Enter submits, the error
 * bound to the form through `aria-describedby` and announced by
 * `role="alert"`, and focus returned to the email field on failure so a
 * keyboard user is where they need to be rather than at the top of the
 * document.
 */

const FAILURES: Record<string, TranslationKey> = {
  invalid_credentials: "login.invalid",
  rate_limited: "login.rateLimited",
  forbidden: "login.refused",
  unavailable: "auth.failed",
};

export function LoginPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const search = useSearch({ strict: false }) as { next?: string };

  const emailId = useId();
  const passwordId = useId();
  const errorId = useId();

  const emailField = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<TranslationKey | null>(null);

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;

    const form = new FormData(event.currentTarget);
    setBusy(true);
    setFailure(null);

    const outcome = await signIn(String(form.get("email")), String(form.get("password")));
    setBusy(false);

    if (outcome.status !== "ok") {
      setFailure(FAILURES[outcome.status] ?? "auth.failed");
      // Back to the first field: a failure that leaves focus on a disabled
      // button strands a keyboard user.
      emailField.current?.focus();
      return;
    }

    accessToken.set(outcome.value);
    // `safeRedirect` decides, not the query string — §8. An external URL,
    // a protocol-relative one, or a loop back to this page all become the
    // dashboard.
    void navigate({ to: safeRedirect(search.next), replace: true });
  };

  return (
    <main className="gate">
      <h1>{t("login.title")}</h1>
      <p className="muted">{t("login.subtitle")}</p>

      <form onSubmit={(event) => void onSubmit(event)} noValidate>
        <p className="field">
          <label htmlFor={emailId}>{t("login.email")}</label>
          <input
            ref={emailField}
            id={emailId}
            name="email"
            type="email"
            autoComplete="username"
            required
            aria-describedby={failure ? errorId : undefined}
          />
        </p>

        <p className="field">
          <label htmlFor={passwordId}>{t("login.password")}</label>
          <input
            id={passwordId}
            name="password"
            type="password"
            autoComplete="current-password"
            required
            aria-describedby={failure ? errorId : undefined}
          />
        </p>

        {failure !== null && (
          <p id={errorId} role="alert" className="error">
            {t(failure)}
          </p>
        )}

        <button type="submit" className="action" disabled={busy}>
          {t(busy ? "login.submitting" : "login.submit")}
        </button>
      </form>
    </main>
  );
}
