import { useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  type ReactNode,
  use,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import type { SessionState } from "@/entities/session";
import type { User } from "@/entities/user";
import * as authApi from "@/features/auth/api";
import { type AuthChannel, createAuthChannel } from "@/features/auth/model/auth-channel";
import { isSessionEnded } from "@/features/auth/model/error-messages";
import { installAuthInterceptors } from "@/features/auth/model/refresh-interceptor";
import { createSessionStore, type SessionStore } from "@/features/auth/model/session-store";
import { ApiError } from "@/shared/api";
import { reportError } from "@/shared/lib/report-error";

/**
 * Session lifecycle, and nothing else.
 *
 * Deliberately narrow: this owns *who is signed in* and the four operations
 * that change it. It is not a place for UI state, feature flags or a
 * user's preferences — a "global store" that starts as a session provider
 * is how every app ends up with one context nothing can be tested without.
 *
 * ## Bootstrap
 *
 * On load the app has no access token — it is never persisted — so the
 * first thing it does is ask the `HttpOnly` cookie for one. Three outcomes,
 * and the third is the one usually got wrong:
 *
 *   200            authenticated, token in memory
 *   401            anonymous — there was no session, which is a fact
 *   network/5xx    **unavailable**, not anonymous
 *
 * A server that could not be reached has not established that the user is
 * signed out. Rendering the login page there discards a live session over
 * one failed request, and the user re-authenticates for no reason.
 *
 * ## Strict Mode
 *
 * React 19 runs effects twice in development. Two bootstraps means two
 * refreshes, and because the backend rotates on every use, the second
 * presents a token the first just superseded — which revokes the session.
 * `bootstrapped` is a ref, checked before the call, so exactly one runs.
 * This is not a development-only nicety: the same double-call happens for
 * real whenever an effect's dependencies churn.
 */
interface SessionContextValue {
  state: SessionState;
  signIn: (credentials: { email: string; password: string }) => Promise<void>;
  signUp: (payload: SignUpPayload) => Promise<void>;
  signOut: () => Promise<void>;
  signOutEverywhere: () => Promise<void>;
  /** Retries the bootstrap after an `unavailable` result. */
  retryBootstrap: () => void;
  /**
   * Replaces the cached account with a **server** answer — A64-021.5H §22.
   *
   * One caller: the verification page, holding the `UserRead` that
   * `POST /auth/email/verify-code` returned. That response is the
   * authoritative current user, so applying it is how `is_verified` becomes
   * true everywhere — the route guard, the header, and any page that reads
   * it — without a second request and without the frontend deciding
   * anything.
   *
   * Deliberately takes a whole `User` rather than a flag. A
   * `markVerified()` would let a client set a state the server never
   * reported, which is precisely what §26 forbids.
   */
  applyUser: (user: User) => void;
}

/**
 * What a registration form collects.
 *
 * `preferred_language` and `timezone` are **not** collected: the browser
 * already knows both, and asking a person to pick their own timezone from
 * a list of four hundred at sign-up is a form field nobody wants. They are
 * filled in from the environment below and are editable later in settings.
 * `display_name` is omitted entirely — the backend defaults it, and a
 * second name field at sign-up is a decision to defer to the Profile phase.
 */
export interface SignUpPayload {
  email: string;
  username: string;
  password: string;
  preferredLanguage: "uz" | "ru" | "en";
}

const SessionContext = createContext<SessionContextValue | null>(null);

/**
 * What must be torn down when a session ends, beyond this module's own
 * state — the WebSocket, principally.
 *
 * An extension point rather than an import, because `features/auth` must
 * not know that a realtime connection exists: that is another feature's,
 * and a session provider that imported it would couple sign-out to
 * gameplay. Whoever owns the socket registers here.
 */
const cleanups = new Set<() => void>();

export function onSessionEnded(cleanup: () => void): () => void {
  cleanups.add(cleanup);
  return () => {
    cleanups.delete(cleanup);
  };
}

function runCleanups(): void {
  for (const cleanup of cleanups) {
    try {
      cleanup();
    } catch (error) {
      // One misbehaving listener must not stop the others, and must not
      // stop the sign-out — CLAUDE.md §9.2, applied to teardown.
      reportError(error, { scope: "session-cleanup" });
    }
  }
}

/**
 * What must be released **while the session still works** — A64-021.6 §23.
 *
 * A second extension point, and the difference from `onSessionEnded` above
 * is the only reason it exists: these run *before* the server is told to
 * end the session, and they may be async.
 *
 * The push subscription is why. It is bound to the browser rather than to
 * the tab, so it outlives a sign-out unless something removes it — and
 * removing the backend's record of it needs the session that is about to be
 * revoked. A cleanup that ran afterwards would be calling an endpoint with
 * a dead token, and the row would stay live, pointed at the previous account
 * on a browser somebody else is about to use.
 *
 * An extension point rather than an import, for the same reason as
 * `onSessionEnded`: `features/auth` must not know that push exists.
 */
const releases = new Set<() => Promise<void>>();

export function onSessionEnding(release: () => Promise<void>): () => void {
  releases.add(release);
  return () => {
    releases.delete(release);
  };
}

async function runReleases(): Promise<void> {
  // `allSettled`, never `all`: one release that rejects must not stop the
  // others and must not stop the sign-out. Somebody who asked to be signed
  // out is signed out, whatever a push service said.
  const outcomes = await Promise.allSettled([...releases].map((release) => release()));
  for (const outcome of outcomes) {
    if (outcome.status === "rejected") {
      reportError(outcome.reason, { scope: "session-release" });
    }
  }
}

export function SessionProvider({
  children,
  store: injectedStore,
  channel: injectedChannel,
}: {
  children: ReactNode;
  store?: SessionStore;
  channel?: AuthChannel;
}) {
  const [store] = useState(() => injectedStore ?? createSessionStore());
  const [channel] = useState(() => injectedChannel ?? createAuthChannel());
  const queryClient = useQueryClient();
  const bootstrapped = useRef(false);

  const state = useSyncExternalStore(store.subscribe, store.getState, store.getState);

  /**
   * Clears everything this device holds. Never fails, never awaits.
   *
   * ## It does nothing when there was nothing — A64-026.5 §44.2
   *
   * This runs when a refresh fails, and a refresh fails for two different
   * reasons that used to be treated as one: a session that ended, and a
   * session that never existed. The second happens on every public page,
   * because a `401` from any endpoint that needs an account routes through
   * the same interceptor.
   *
   * Treating it as an ending made a loop with no exit. Clearing calls
   * `removeQueries`, removing a query refetches it in every component
   * still mounted, the refetch `401`s, and the `401` clears again —
   * measured at roughly 175 requests a second on `/players/{username}`,
   * for as long as the tab stayed open.
   *
   * So a viewer who is already anonymous has nothing to release: no cached
   * response was fetched as anybody, no listener holds a session, and the
   * state is already `anonymous`. The guard is the exit.
   */
  const clearLocalSession = useCallback(() => {
    if (store.getState().status === "anonymous") return;

    store.set({ status: "anonymous" });
    runCleanups();
    // Every cached query was fetched **as somebody**. Leaving them would
    // show the previous user's data to whoever signs in next on this
    // device — `removeQueries`, not `invalidateQueries`, because
    // invalidation keeps the data and refetches it.
    queryClient.removeQueries();
  }, [queryClient, store]);

  const bootstrap = useCallback(async () => {
    store.set({ status: "bootstrapping" });
    try {
      const session = await authApi.refresh();
      store.set({
        status: "authenticated",
        user: session.user,
        accessToken: session.access_token,
      });
    } catch (error) {
      if (isSessionEnded(error)) {
        // There was no session. A fact, not a failure.
        store.set({ status: "anonymous" });
        return;
      }
      // The server could not be reached, or answered with something this
      // app cannot interpret. Saying "anonymous" here would be a guess.
      reportError(error, { scope: "session-bootstrap" });
      store.set({ status: "unavailable" });
    }
  }, [store]);

  // --- one bootstrap, one interceptor pair, one subscription -------------
  useEffect(() => {
    if (bootstrapped.current) return;
    bootstrapped.current = true;
    void bootstrap();
  }, [bootstrap]);

  useEffect(() => {
    return installAuthInterceptors(store, {
      refresh: async () => {
        const session = await authApi.refresh();
        store.set({
          status: "authenticated",
          user: session.user,
          accessToken: session.access_token,
        });
        return session.access_token;
      },
      onSessionEnded: clearLocalSession,
    });
  }, [clearLocalSession, store]);

  useEffect(() => {
    // Another tab signed out. This one clears without calling the server:
    // the session is already revoked, and a second call would be a
    // redundant request that can only fail.
    return channel.subscribe(clearLocalSession);
  }, [channel, clearLocalSession]);

  useEffect(() => () => channel.close(), [channel]);

  const signIn = useCallback(
    async (credentials: { email: string; password: string }) => {
      const session = await authApi.login(credentials);
      store.set({
        status: "authenticated",
        user: session.user,
        accessToken: session.access_token,
      });
    },
    [store],
  );

  const signUp = useCallback(
    async (payload: SignUpPayload) => {
      const session = await authApi.register({
        email: payload.email,
        username: payload.username,
        password: payload.password,
        preferred_language: payload.preferredLanguage,
        // The browser's own zone. `UTC` when the runtime cannot say, which
        // matches the backend's default rather than guessing a region.
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      });
      store.set({
        status: "authenticated",
        user: session.user,
        accessToken: session.access_token,
      });
    },
    [store],
  );

  const signOut = useCallback(async () => {
    let failure: unknown = null;
    // **A64-021.6 §23 — before the session ends, and this ordering is the
    // whole cross-account defence.**
    //
    // A push subscription is bound to the *browser*, not the tab, and it
    // outlives a sign-out unless something removes it. Leaving one behind
    // means the next person to sign in on this laptop shares a browser with
    // a live capability pointing at the previous account — and the delivery
    // worker would keep pushing to it.
    //
    // It runs **first** because removing the backend record needs the
    // session that is about to be revoked. Best-effort: a network failure
    // here must not leave somebody signed in, and the browser-side
    // `unsubscribe()` has already happened by then, so the worst outcome is
    // a stored row that answers `410` on its next delivery and is revoked
    // automatically (§17).
    await runReleases();
    try {
      await authApi.logout();
    } catch (error) {
      // **Local cleanup happens regardless.** A network failure must never
      // leave a user looking signed in when they asked not to be; the
      // server-side session outlives it and the cookie is cleared on the
      // next successful call, which is the lesser of the two wrongs.
      failure = error;
    }
    clearLocalSession();
    channel.post({ type: "logged_out" });
    if (failure !== null) {
      reportError(failure, { scope: "sign-out" });
      throw failure instanceof ApiError ? failure : new Error("Sign-out failed remotely.");
    }
  }, [channel, clearLocalSession]);

  const signOutEverywhere = useCallback(async () => {
    // The same reason as `signOut`. This device is one of the ones being
    // signed out, and it is the only one whose browser subscription this
    // code can reach — the others are revoked when their own sessions end,
    // or by a `410` on the next delivery.
    await runReleases();
    try {
      await authApi.logoutEverywhere();
    } finally {
      clearLocalSession();
      channel.post({ type: "logged_out" });
    }
  }, [channel, clearLocalSession]);

  const applyUser = useCallback(
    (user: User) => {
      // A no-op unless there is a session to apply it to: a response that
      // arrived after a sign-out must not resurrect one.
      const current = store.getState();
      if (current.status !== "authenticated") return;
      store.set({ ...current, user });
    },
    [store],
  );

  const value = useMemo<SessionContextValue>(
    () => ({
      state,
      signIn,
      signUp,
      signOut,
      signOutEverywhere,
      retryBootstrap: () => void bootstrap(),
      applyUser,
    }),
    [applyUser, bootstrap, signIn, signOut, signOutEverywhere, signUp, state],
  );

  return <SessionContext value={value}>{children}</SessionContext>;
}

/** Throws outside the provider — see `useTheme` on why not a silent default. */
export function useSession(): SessionContextValue {
  const value = use(SessionContext);
  if (value === null) {
    throw new Error("useSession must be used inside a SessionProvider.");
  }
  return value;
}
