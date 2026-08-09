import { useCallback, useEffect, useState } from "react";

import { accessToken } from "@/app/session-store";
import { type AdminSession, fetchAdminSession, refresh } from "@/shared/api/client";

/**
 * The authorization state every protected route reads — A64-024.2 §7, §10.
 *
 * ## It starts as `checking` and never as authorized
 *
 * There is no optimistic branch, no cached role and no `useState(true)` in
 * this app. The first paint of a direct navigation or a refresh is the
 * checking state, so privileged chrome cannot flash before the server has
 * answered (§16).
 *
 * ## Refresh comes first, and that is what makes a reload work
 *
 * A reload loses the in-memory access token and keeps the `HttpOnly`
 * refresh cookie. So the sequence is: if there is no token, try to trade
 * the cookie for one; then ask `/admin/me`. Without the first step every
 * refresh on `/users` would land on the login form despite a live session.
 *
 * ## Authorization is never cached
 *
 * The check re-runs on **every protected navigation** — `routeKey` is a
 * dependency of the effect. A revoked administrator is therefore refused
 * the next time they move within the console, not after a reload:
 * A64-024.1 put the role in the database rather than in the token
 * precisely so that this can be true, and caching the answer in client
 * state would have thrown that away.
 */
export type AdminAuth =
  | { state: "checking" }
  | { state: "authorized"; session: AdminSession }
  /** Signed in, and not an administrator. */
  | { state: "forbidden" }
  | { state: "unauthenticated" }
  | { state: "unavailable" };

export function useAdminAuth(routeKey: string): {
  auth: AdminAuth;
  resolve: () => void;
  forget: () => void;
} {
  const [auth, setAuth] = useState<AdminAuth>({ state: "checking" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;

    const run = async () => {
      if (accessToken.get() === null) {
        const renewed = await refresh();
        if (cancelled) return;
        if (renewed.status === "ok") {
          accessToken.set(renewed.value);
        } else if (renewed.status === "unavailable") {
          setAuth({ state: "unavailable" });
          return;
        } else {
          // No cookie, or one the server refused. Not an error — it is
          // simply somebody who has not signed in.
          setAuth({ state: "unauthenticated" });
          return;
        }
      }

      const session = await fetchAdminSession();
      if (cancelled) return;

      if (session.status === "ok") {
        setAuth({ state: "authorized", session: session.value });
        return;
      }
      if (session.status === "unauthenticated") {
        // The token we held is dead. Drop it so the next resolve starts
        // from the cookie rather than replaying a token the server refused.
        accessToken.clear();
        setAuth({ state: "unauthenticated" });
        return;
      }
      setAuth({ state: session.status === "forbidden" ? "forbidden" : "unavailable" });
    };

    void run();
    return () => {
      cancelled = true;
    };
    // `routeKey` is in the dependency list, so **every protected
    // navigation re-asks the server** — §10. Without it the layout stays
    // mounted across sections and a role revoked mid-session would not be
    // noticed until a full reload, which is exactly the indefinite client
    // cache §10 forbids.
  }, [attempt, routeKey]);

  const resolve = useCallback(() => {
    setAuth({ state: "checking" });
    setAttempt((current) => current + 1);
  }, []);

  const forget = useCallback(() => {
    accessToken.clear();
    setAuth({ state: "unauthenticated" });
  }, []);

  return { auth, resolve, forget };
}
