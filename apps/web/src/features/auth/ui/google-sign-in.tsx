import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { googleStartUrl, signInProviders } from "@/features/auth/api";
import { authKeys } from "@/features/auth/api/keys";
import { messageKeyFor } from "@/features/auth/model/error-messages";
import { safeRedirect } from "@/features/auth/model/safe-redirect";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { buttonVariants, Notice, Spinner } from "@/shared/ui";

/**
 * "Continue with Google" — A64-031.E1.
 *
 * ## A link, not a button
 *
 * Starting the flow is a navigation to the API, which redirects to Google:
 * there is no request for this page to make and no response for it to
 * read. An `<a>` is that, natively — keyboard, middle-click and no-script
 * all work — and nothing about the sign-in passes through script.
 *
 * ## Shown only when the deployment offers it
 *
 * `GET /auth/browser/providers` says whether this tier holds a Google
 * client. Until it answers, and if it fails, nothing renders: a button
 * that leads to "Google sign-in is not available" is worse than no button,
 * and the email form beneath works either way.
 *
 * ## One click
 *
 * After the first activation the link is inert and shows progress, so a
 * double click cannot start two flows. `pageshow` re-arms it when the
 * browser restores this page from its back/forward cache — somebody who
 * pressed Back at Google must not find a spinner that never ends.
 */
export function GoogleSignIn({ next }: { next: string | undefined }) {
  const { t } = useTranslation();
  const [leaving, setLeaving] = useState(false);
  const providers = useQuery({
    queryKey: authKeys.providers(),
    queryFn: signInProviders,
    // Configuration, not data: it changes with a deploy, not a session.
    staleTime: Infinity,
    retry: false,
  });

  useEffect(() => {
    const rearm = (event: PageTransitionEvent) => {
      if (event.persisted) setLeaving(false);
    };
    window.addEventListener("pageshow", rearm);
    return () => window.removeEventListener("pageshow", rearm);
  }, []);

  if (providers.data?.google !== true) return null;

  return (
    <div className="flex flex-col gap-4">
      <a
        href={googleStartUrl(safeRedirect(next))}
        aria-disabled={leaving || undefined}
        onClick={(event) => {
          if (leaving) {
            event.preventDefault();
            return;
          }
          setLeaving(true);
        }}
        className={cn(
          buttonVariants({ variant: "outline" }),
          "w-full",
          leaving && "pointer-events-none opacity-70",
        )}
      >
        {leaving ? (
          <Spinner label={t("auth.google.redirecting")} />
        ) : (
          <>
            <GoogleMark />
            {t("auth.google.continue")}
          </>
        )}
      </a>
      <div className="text-muted-foreground flex items-center gap-3 text-xs" aria-hidden="true">
        <span className="bg-border h-px flex-1" />
        {t("auth.google.or")}
        <span className="bg-border h-px flex-1" />
      </div>
    </div>
  );
}

/**
 * Google's "G", in Google's colours.
 *
 * The one place the platform's colour tokens do not apply: Google's
 * branding guidelines require the mark as published, in either theme.
 * Decorative — the link's text names the action.
 */
function GoogleMark() {
  return (
    <svg viewBox="0 0 48 48" aria-hidden="true" focusable="false" className="size-5">
      <path
        fill="#EA4335"
        d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"
      />
      <path
        fill="#4285F4"
        d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"
      />
      <path
        fill="#FBBC05"
        d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"
      />
      <path
        fill="#34A853"
        d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"
      />
    </svg>
  );
}

/**
 * What a Google round trip that did not sign anybody in came back to say —
 * `/login?google=<reason>`, written by the API's callback.
 *
 * A closed table: an unknown reason renders nothing rather than a raw
 * string, because the query is the URL's and anybody can type one.
 * `cancelled` is information, not an error — the person chose it.
 */
const OUTCOMES: Record<string, { key: TranslationKey; tone: "info" | "error" }> = {
  cancelled: { key: "auth.google.outcome.cancelled", tone: "info" },
  failed: { key: "auth.google.outcome.failed", tone: "error" },
  expired: { key: "auth.google.outcome.expired", tone: "error" },
  email_unverified: { key: "auth.google.outcome.emailUnverified", tone: "error" },
  unavailable: { key: "auth.google.outcome.unavailable", tone: "error" },
  rate_limited: { key: "auth.errors.rate_limited", tone: "error" },
  inactive_account: { key: "auth.errors.inactive_account", tone: "error" },
  account_locked: { key: "auth.errors.account_locked", tone: "error" },
  account_restricted: { key: "auth.google.outcome.accountRestricted", tone: "error" },
};

export function GoogleOutcomeNotice({ outcome }: { outcome: string | undefined }) {
  const { t } = useTranslation();
  // `hasOwn`, not `in`: `?google=constructor` must not find the prototype.
  if (outcome === undefined || !Object.hasOwn(OUTCOMES, outcome)) return null;
  const known = OUTCOMES[outcome];
  if (known === undefined) return null;
  return <Notice tone={known.tone}>{t(known.key)}</Notice>;
}

/**
 * A Google step page with nothing pending — used, timed out, or reached by
 * hand. Says so and points back to the sign-in page, where "Continue with
 * Google" starts afresh; a network failure says that instead. The page
 * supplies the shell around it.
 */
export function GoogleSignInExpired({ error }: { error: unknown }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-4">
      <Notice tone="error">{t(messageKeyFor(error))}</Notice>
      <Link to="/login" className={cn(buttonVariants({ variant: "outline" }), "w-full")}>
        {t("auth.forgotPassword.backToLogin")}
      </Link>
    </div>
  );
}
