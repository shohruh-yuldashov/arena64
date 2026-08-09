import { useCallback, useEffect, useState } from "react";

import {
  type AdminSession,
  fetchAdminSession,
  type SessionOutcome,
} from "@/shared/api/admin-session";

/**
 * The authorization gate's state — A64-024.1 §6.
 *
 * Starts as `checking` and **never** starts as authorized. That is the
 * whole of "loading state must not briefly render privileged admin
 * content": there is no optimistic branch, no cached role and no
 * `useState(true)` anywhere in this app, so the first paint of a direct
 * navigation or a refresh is the checking state and the shell cannot flash.
 */
export type AdminAuth =
  | { state: "checking" }
  | { state: "authorized"; session: AdminSession }
  | { state: "forbidden" }
  | { state: "unauthenticated" }
  | { state: "unavailable" };

const OUTCOMES: Record<SessionOutcome["status"], AdminAuth["state"]> = {
  authorized: "authorized",
  forbidden: "forbidden",
  unauthenticated: "unauthenticated",
  unavailable: "unavailable",
};

export function useAdminSession(): { auth: AdminAuth; recheck: () => void } {
  const [auth, setAuth] = useState<AdminAuth>({ state: "checking" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;

    void fetchAdminSession(controller.signal).then((outcome) => {
      if (cancelled) return;
      setAuth(
        outcome.status === "authorized"
          ? { state: "authorized", session: outcome.session }
          : { state: OUTCOMES[outcome.status] as Exclude<AdminAuth["state"], "authorized"> },
      );
    });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [attempt]);

  // Re-asks the server. The only way authorization changes in this app —
  // there is no local mutation of it, by design.
  const recheck = useCallback(() => {
    setAuth({ state: "checking" });
    setAttempt((current) => current + 1);
  }, []);

  return { auth, recheck };
}
