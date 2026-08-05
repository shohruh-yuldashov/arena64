import type { User } from "@/entities/user";
import type { components } from "@/shared/api/generated/schema";

/** The browser session as `POST /auth/browser/*` returns it. Generated. */
export type BrowserSession = components["schemas"]["BrowserSession"];

/**
 * Where the app is in its session lifecycle.
 *
 * A **discriminated union**, not a `user | null` beside an `isLoading`
 * boolean. Three states exist and only three, and the pair-of-flags
 * encoding admits four — including "loading and authenticated", which is
 * meaningless and which some component would eventually render.
 *
 * `bootstrapping` matters most: before the first refresh completes, the app
 * does not know whether it is signed in. Treating that as `anonymous`
 * would flash the login page at every returning player on every reload,
 * and a guard that redirected on it would send them there for real.
 *
 * `unavailable` is deliberately distinct from `anonymous`. A refresh that
 * failed because the network is down has not established that the user is
 * signed out — pretending otherwise discards a live session over one
 * failed request.
 */
export type SessionState =
  | { status: "bootstrapping" }
  | { status: "anonymous" }
  | { status: "authenticated"; user: User; accessToken: string }
  | { status: "unavailable" };

export function isAuthenticated(
  state: SessionState,
): state is Extract<SessionState, { status: "authenticated" }> {
  return state.status === "authenticated";
}

/** Whether a guard may act yet. Nothing should redirect while unresolved. */
export function isResolved(state: SessionState): boolean {
  return state.status !== "bootstrapping";
}
