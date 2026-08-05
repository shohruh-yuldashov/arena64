import axios, { type AxiosError, type InternalAxiosRequestConfig } from "axios";

import { REFRESH_PATH } from "@/features/auth/api";
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
 * ## Why each request retries at most once
 *
 * `_retried` is stamped on the config. Without it, a request that still
 * `401`s after a successful refresh — a genuinely forbidden resource, a
 * revoked account — loops until the browser gives up.
 */
declare module "axios" {
  export interface InternalAxiosRequestConfig {
    /** Set by this interceptor. One retry per request, ever. */
    _retried?: boolean;
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
    (response) => response,
    async (error: unknown) => {
      if (!axios.isAxiosError(error)) throw error;

      const failed = error.config;
      if (
        error.response?.status !== 401 ||
        failed === undefined ||
        failed._retried === true ||
        isRefreshCall(failed)
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

      failed.headers.set("Authorization", `Bearer ${token}`);
      return httpClient.request(failed);
    },
  );

  return () => {
    httpClient.interceptors.request.eject(requestId);
    httpClient.interceptors.response.eject(responseId);
  };
}

/** Whether this config is the refresh call. See the module docstring. */
function isRefreshCall(config: AxiosError["config"] | InternalAxiosRequestConfig): boolean {
  return config?.url?.endsWith(REFRESH_PATH) ?? false;
}
