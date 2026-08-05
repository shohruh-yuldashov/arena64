import { useNavigate, useRouterState } from "@tanstack/react-router";
import { type ReactNode, useEffect } from "react";

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
 *
 * ## Why the redirect is an effect and not `<Navigate>`
 *
 * `<Navigate to="/login" search={{ next }} />` navigates from an effect
 * whose dependency is the options object — and `{ next }` is a **new
 * object on every render**. So the effect re-runs, navigates again,
 * re-renders, and the tab locks up. That is not hypothetical: the first
 * draft did exactly this, and the profile test exhausted the worker's heap
 * before a single assertion ran.
 *
 * An effect with primitive dependencies fires once per decision. The guard
 * renders `null` while it is in flight, because rendering the protected
 * page for one frame is how a signed-out user sees a flash of somebody
 * else's screen.
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
  const navigate = useNavigate();
  const location = useRouterState({ select: (router) => router.location });
  // The **current** path, so signing in returns them where they were going.
  // A string, not an object, so the effect below has a stable dependency.
  const next = `${location.pathname}${location.searchStr}`;
  const shouldRedirect = isResolved(state) && state.status === "anonymous";

  useEffect(() => {
    if (!shouldRedirect) return;
    // `safeRedirect` on the way back out, so a path that arrived here by
    // some other route cannot become an open redirect.
    void navigate({ to: "/login", search: { next }, replace: true });
  }, [navigate, next, shouldRedirect]);

  if (!isResolved(state)) return <Pending />;

  if (state.status === "unavailable") {
    // Not a redirect: the server could not be reached, so nothing has
    // established that this user may not be here. Sending them to `/login`
    // would ask them to re-authenticate over a failed request.
    return <SessionUnavailable />;
  }

  // `null` rather than the page: rendering it for the frame before the
  // navigation lands is a flash of somebody else's screen.
  if (state.status === "anonymous") return null;

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
  const navigate = useNavigate();
  // Where they were originally going, if that survived the round trip;
  // otherwise home. Validated, because `next` came from a query string.
  const destination = safeRedirect(next);
  const shouldRedirect = isResolved(state) && state.status === "authenticated";

  useEffect(() => {
    if (!shouldRedirect) return;
    void navigate({ to: destination as typeof DEFAULT_REDIRECT, replace: true });
  }, [destination, navigate, shouldRedirect]);

  if (!isResolved(state)) return <Pending />;
  if (state.status === "authenticated") return null;

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
