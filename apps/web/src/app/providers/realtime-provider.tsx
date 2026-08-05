import { type ReactNode, useEffect } from "react";

import { isAuthenticated } from "@/entities/session";
import { onSessionEnded, useSession } from "@/features/auth/model/session-provider";
import type { RealtimeClient } from "@/shared/realtime";
import { RealtimeContextProvider, useRealtime } from "@/shared/realtime";

/**
 * What starts and stops the one socket — A64-020.5B §3, §4.
 *
 * The transport lives in `shared/realtime`; this is its **lifecycle**, and
 * it lives in `app/providers` because it is the only layer allowed to know
 * about both a session and a socket (`specs/frontend.md` §3). That is the
 * layering being right rather than a lint rule being appeased: a transport
 * that imported the session could not be tested without one.
 *
 *     authenticated      start — mint a ticket, connect, authenticate
 *     signed out         stop, through `onSessionEnded`
 *     browser offline    the client pauses; `online` resumes it
 *
 * `onSessionEnded` is the seam `SessionProvider` published for exactly this
 * and had no consumer until now. Its docstring says why: *"a session
 * provider that imported [the socket] would couple sign-out to gameplay.
 * Whoever owns the socket registers here."*
 */
export function RealtimeProvider({
  children,
  client,
}: {
  children: ReactNode;
  client?: RealtimeClient;
}) {
  return (
    <RealtimeContextProvider {...(client ? { client } : {})}>
      <RealtimeLifecycle>{children}</RealtimeLifecycle>
    </RealtimeContextProvider>
  );
}

/**
 * A separate component so the effects sit **below** the context that
 * provides the client — a provider cannot consume its own context.
 */
function RealtimeLifecycle({ children }: { children: ReactNode }) {
  const { state } = useSession();
  const realtime = useRealtime();
  const authenticated = isAuthenticated(state);

  useEffect(() => {
    if (!authenticated) return;
    realtime.start();
    // Deliberately no cleanup that stops the socket. A re-render that flips
    // `authenticated` for a frame — which a token refresh does — would
    // otherwise close and reopen the connection, costing a ticket, a
    // handshake and a full resume for nothing. Sign-out is what stops it.
  }, [authenticated, realtime]);

  useEffect(() => onSessionEnded(() => realtime.stop()), [realtime]);

  useEffect(() => {
    const resume = () => realtime.resumeFromOffline();
    window.addEventListener("online", resume);
    return () => window.removeEventListener("online", resume);
  }, [realtime]);

  return <>{children}</>;
}
