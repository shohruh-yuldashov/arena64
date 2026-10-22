import axios, {
  type AxiosError,
  type AxiosResponse,
  CanceledError,
  type InternalAxiosRequestConfig,
} from "axios";

import { CREDENTIAL_PATHS, REFRESH_PATH } from "@/features/auth/api";
import type { SessionStore } from "@/features/auth/model/session-store";
import { httpClient } from "@/shared/api/client";

/**
 * Attach the token, and recover from one expiry — A64-020.2 §8.
 *
 * ## Why the refresh must be single-flight
 *
 * The backend **rotates** the refresh token on every use, and presenting an
 * already-rotated one is indistinguishable from a replay, so it revokes the
 * entire session chain (`SPEC-AUTH`, `SessionService.rotate_refresh_token`).
 *
 * A page that loads five widgets at once and gets five `401`s would, with
 * the naive implementation, fire five refreshes. The first rotates the
 * cookie; the other four present the token it just superseded; the session
 * is destroyed and the user is thrown out — by their own app, on a normal
 * page load, intermittently enough to be unreproducible.
 *
 * So exactly one refresh runs. `inFlight` holds its promise, every
 * concurrent `401` awaits the same one, and the winner's result is what
 * they all retry with. That is the whole reason this module is not four
 * lines.
 *
 * ## Why the refresh call itself is excluded
 *
 * A `401` from `/auth/browser/refresh` means the cookie is gone. Retrying
 * it through this interceptor would refresh in order to refresh, forever.
 * `isRefreshCall` is checked first, before anything else.
 *
 * ## Why a sign-in is excluded too — A64-031.E3
 *
 * A `401` from `/auth/browser/login` is the answer to the credentials in
 * its body, not to an access token, so there is nothing a refresh could
 * repair. Treating it as an expiry used to refresh anyway: on the sign-in
 * page there is usually no cookie, the refresh failed with
 * `invalid_session`, and *that* error — not the login's
 * `invalid_credentials` — reached the form as "your session is not
 * valid". With a stale cookie present it was worse: the refresh succeeded
 * and signed the browser into whichever account the cookie belonged to.
 * `CREDENTIAL_PATHS` lists every endpoint whose `401` is about what was
 * submitted.
 *
 * ## Why each request retries at most once
 *
 * `_retried` is stamped on the config. Without it, a request that still
 * `401`s after a successful refresh — a genuinely forbidden resource, a
 * revoked account — loops until the browser gives up.
 *
 * ## A response belongs to the account that asked — A64-031.E5
 *
 * The refresh cookie is shared by every tab, so a sign-in in another tab can
 * make this tab's next refresh answer for a **different** account. Two
 * things must not survive that switch:
 *
 * - **A response to the previous account.** Every request sent while signed
 *   in is stamped with the account it was sent as (`_sentAs`). If it comes
 *   back after the tab has become somebody else — or nobody — it is turned
 *   into a cancellation, so no `onSuccess`, no `setQueryData` and no
 *   component state can apply one account's data to another. "Canceled" is
 *   the existing kind for a superseded request: it is not reported and not
 *   retried.
 * - **A retry under the new account.** A `401` that is answered by a
 *   refresh naming a different account is not re-sent: an action somebody
 *   took as one account must not be replayed as another. It is cancelled
 *   the same way.
 *
 * Only signed-in requests are stamped. An anonymous request reads nothing
 * private, and the session provider clears what it cached when an account
 * arrives.
 */
declare module "axios" {
  export interface InternalAxiosRequestConfig {
    /** Set by this interceptor. One retry per request, ever. */
    _retried?: boolean;
    /** The account this request was first sent as — see the module docstring. */
    _sentAs?: string;
  }
}

export interface RefreshHandlers {
  /** Performs the refresh. Returns the new access token, or throws. */
  refresh: () => Promise<string>;
  /** Called once when the session is definitively over. */
  onSessionEnded: () => void;
}

/**
 * Registers both interceptors and returns a function that removes them.
 *
 * Returned rather than global, because a test — and a future sign-out that
 * rebuilt the client — must be able to undo it. Registering twice would
 * mean two refreshes per `401`, which is precisely the defect above.
 */
export function installAuthInterceptors(
  store: SessionStore,
  handlers: RefreshHandlers,
): () => void {
  let inFlight: Promise<string> | null = null;

  /** The one refresh. Concurrent callers get the same promise. */
  function refreshOnce(): Promise<string> {
    inFlight ??= handlers.refresh().finally(() => {
      // Cleared on **both** paths, so a failed refresh does not leave a
      // rejected promise cached — every later request would inherit that
      // rejection and the app would never recover without a reload.
      inFlight = null;
    });
    return inFlight;
  }

  const requestId = httpClient.interceptors.request.use(
    (config: InternalAxiosRequestConfig) => {
      // Stamped once, on the first send: a retry keeps the account the
      // request was originally made for.
      const session = store.getState();
      if (config._sentAs === undefined && session.status === "authenticated") {
        config._sentAs = session.user.id;
      }
      const token = store.getAccessToken();
      // Read at send time, never captured at registration: the token
      // rotates and a closed-over copy is the divergence this design
      // exists to prevent.
      if (token !== null && !config.headers.has("Authorization")) {
        config.headers.set("Authorization", `Bearer ${token}`);
      }
      return config;
    },
  );

  const responseId = httpClient.interceptors.response.use(
    (response: AxiosResponse) => {
      if (isSuperseded(response.config)) throw superseded();
      return response;
    },
    async (error: unknown) => {
      if (!axios.isAxiosError(error)) throw error;

      const failed = error.config;
      if (
        error.response?.status !== 401 ||
        failed === undefined ||
        failed._retried === true ||
        isRefreshCall(failed) ||
        isCredentialCall(failed)
      ) {
        throw error;
      }

      failed._retried = true;

      let token: string;
      try {
        token = await refreshOnce();
      } catch (refreshFailure) {
        // The cookie is gone, revoked or rotated away. This is the one
        // place that decides a session is over, and it says so once —
        // every queued request simply fails with its own original error.
        handlers.onSessionEnded();
        throw refreshFailure;
      }

      // The refresh may have answered for somebody else. An action taken as
      // one account is never replayed as another.
      if (isSuperseded(failed)) throw superseded();

      failed.headers.set("Authorization", `Bearer ${token}`);
      return httpClient.request(failed);
    },
  );

  /** Whether this request was sent as an account the tab no longer holds. */
  function isSuperseded(config: InternalAxiosRequestConfig): boolean {
    if (config._sentAs === undefined) return false;
    const session = store.getState();
    return session.status !== "authenticated" || session.user.id !== config._sentAs;
  }

  return () => {
    httpClient.interceptors.request.eject(requestId);
    httpClient.interceptors.response.eject(responseId);
  };
}

/** Whether this config is the refresh call. See the module docstring. */
function isRefreshCall(config: AxiosError["config"] | InternalAxiosRequestConfig): boolean {
  return config?.url?.endsWith(REFRESH_PATH) ?? false;
}

/** Whether this config submits credentials. See the module docstring. */
function isCredentialCall(config: InternalAxiosRequestConfig): boolean {
  const path = config.url;
  return path !== undefined && CREDENTIAL_PATHS.some((credential) => path.endsWith(credential));
}

/** A cancellation, which is what a response to a superseded account is. */
function superseded(): CanceledError<unknown> {
  return new CanceledError("The signed-in account changed while this request was in flight.");
}
