import { Navigate, useRouterState } from "@tanstack/react-router";
import type { ReactNode } from "react";

import { isResolved } from "@/entities/session";
import { DEFAULT_REDIRECT, safeRedirect } from "@/features/auth/model/safe-redirect";
import { useSession } from "@/features/auth/model/session-provider";
import { useTranslation } from "@/shared/i18n";
import { Button, Spinner } from "@/shared/ui";

/**
 * Who may see what — A64-020.2 §10.
 *
 * ## Why these are components and not `beforeLoad`
 *
 * TanStack Router's `beforeLoad` runs before React renders, which is the
 * natural place for a guard and the wrong one here: the session lives in a
 * React context that does not exist yet at that point, and threading it
 * through the router's context would mean the router owning session state.
 * Rendering the guard means it reads the same context every component does,
 * and re-evaluates when the session changes — which is what makes a
 * sign-out in another tab actually move this tab off a protected page.
 *
 * ## Nothing redirects while the session is unresolved
 *
 * `bootstrapping` is not `anonymous`. A guard that redirected on it would
 * send every returning player to `/login` on every reload, for real — the
 * refresh has not answered yet. So both guards render a pending state until
 * `isResolved`, and only then decide.
 */
function Pending() {
  const { t } = useTranslation();
  return (
    <div className="flex justify-center py-24">
      <Spinner label={t("common.loading")} />
    </div>
  );
}

/**
 * A page only a signed-in player may see.
 *
 * **Nothing uses this yet**, and that is deliberate: no application page
 * exists in this phase. It ships now because the alternative is every
 * later phase inventing its own, and because `App.test.tsx` can prove it
 * works before there is a screen behind it.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { state } = useSession();
  const location = useRouterState({ select: (router) => router.location });

  if (!isResolved(state)) return <Pending />;

  if (state.status === "unavailable") {
    // Not a redirect: the server could not be reached, so nothing has
    // established that this user may not be here. Sending them to `/login`
    // would ask them to re-authenticate over a failed request.
    return <SessionUnavailable />;
  }

  if (state.status === "anonymous") {
    // The **current** path, so signing in returns them where they were
    // going. Passed through `safeRedirect` on the way back out, so a path
    // that arrived here by some other route cannot become an open redirect.
    const next = `${location.pathname}${location.searchStr}`;
    return <Navigate to="/login" search={{ next }} replace />;
  }

  return <>{children}</>;
}

/**
 * A page only an anonymous visitor should see — `/login`, `/register`.
 *
 * A signed-in player who follows a bookmark to `/login` wants the app, not
 * a form asking them to become someone they already are.
 */
export function RequireAnonymous({
  children,
  next,
}: {
  children: ReactNode;
  next?: string | undefined;
}) {
  const { state } = useSession();

  if (!isResolved(state)) return <Pending />;

  if (state.status === "authenticated") {
    // Where they were originally going, if that survived the round trip;
    // otherwise home. Validated, because `next` came from a query string.
    return <Navigate to={safeRedirect(next) as typeof DEFAULT_REDIRECT} replace />;
  }

  return <>{children}</>;
}

/** The bounded, recoverable state a failed bootstrap lands in. */
function SessionUnavailable() {
  const { t } = useTranslation();
  const { retryBootstrap } = useSession();

  return (
    <div
      role="alert"
      className="mx-auto flex max-w-md flex-col items-center gap-4 py-24 text-center"
    >
      <h1 className="text-xl font-semibold">{t("auth.session.unavailableTitle")}</h1>
      <p className="text-muted-foreground text-sm">{t("auth.session.unavailableBody")}</p>
      <Button onClick={retryBootstrap}>{t("auth.session.retry")}</Button>
    </div>
  );
}
