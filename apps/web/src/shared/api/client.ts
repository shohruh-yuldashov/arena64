import axios, { type AxiosInstance, type InternalAxiosRequestConfig } from "axios";

import { CORRELATION_ID_HEADER } from "@/shared/config/constants";
import { env } from "@/shared/config/env";

/**
 * The one HTTP client for the platform's API.
 *
 * ## Why an instance rather than the `axios` default export
 *
 * A module-level `axios.get(...)` shares interceptors with every other
 * library in the bundle that does the same. This instance is private to
 * Arena64: an interceptor registered here cannot be observed or removed by
 * anything else, and a test can build a second instance without disturbing
 * the first.
 *
 * ## Extension points, and what is deliberately not here
 *
 * `withAuthorization` and `onUnauthorized` are the two seams
 * authentication will need, and both are **empty** in this phase — A64-020.1
 * builds infrastructure, not sessions. What matters is that adding a
 * credential later is a registration at the seam rather than an edit to
 * every call site.
 *
 * There is **no token refresh here and no token storage anywhere in this
 * app.** The approved F-1 design keeps the access token in memory and the
 * refresh token in an `HttpOnly` cookie, and it assumed a Next.js Route
 * Handler to set that cookie — which this Vite client does not have. That
 * is an open question recorded in `specs/frontend.md`, not a decision this
 * phase makes by default. Nothing here reads or writes `localStorage`.
 */
export const httpClient: AxiosInstance = axios.create({
  baseURL: env.VITE_API_URL,
  // Every remote call has a timeout — CLAUDE.md §3.7. A request without
  // one hangs until the browser gives up, which on a flaky mobile network
  // is minutes of a spinner nobody can cancel.
  timeout: 15_000,
  headers: { "Content-Type": "application/json" },
  // The refresh cookie, when one exists, is `HttpOnly` and same-site; the
  // client must be willing to send it. Harmless today (there is no cookie)
  // and one less thing to remember when there is.
  withCredentials: true,
});

/**
 * Every request carries a correlation id unless the caller set one —
 * echoed by `apps/api/app/common/middleware.py`'s `CorrelationIdMiddleware`,
 * so a request that fails can be traced through backend logs from the
 * moment this client sent it.
 */
httpClient.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  if (!config.headers.has(CORRELATION_ID_HEADER)) {
    config.headers.set(CORRELATION_ID_HEADER, crypto.randomUUID());
  }
  return config;
});

/** Produces the value of the `Authorization` header, or `null` for none. */
export type AuthorizationSource = () => string | null;

/**
 * Registers the credential source. **The extension point, not the
 * implementation** — authentication registers one of these once it exists,
 * and nothing else in this module changes.
 *
 * Returns a function that removes the interceptor, so a test or a sign-out
 * can undo it without rebuilding the client.
 */
export function withAuthorization(source: AuthorizationSource): () => void {
  const id = httpClient.interceptors.request.use((config) => {
    const credential = source();
    if (credential !== null) {
      config.headers.set("Authorization", credential);
    }
    return config;
  });
  return () => httpClient.interceptors.request.eject(id);
}

/**
 * Registers what to do when the API says the caller is not authenticated.
 *
 * The second half of the seam above, and separate on purpose: refreshing a
 * credential and *attaching* one are different concerns, and a single
 * "auth interceptor" that did both is how retry loops are born. The
 * rejection is always re-thrown — this observes, it does not swallow.
 */
export function onUnauthorized(handle: () => void): () => void {
  const id = httpClient.interceptors.response.use(
    (response) => response,
    (error: unknown) => {
      if (axios.isAxiosError(error)) {
        if (error.response?.status === 401) {
          handle();
        }
        throw error;
      }
      // Anything that is not an `AxiosError` reaching here is a defect in
      // this app rather than a response. Wrapped rather than re-thrown raw
      // so the rejection is always an `Error` with the original as its
      // cause — CLAUDE.md §9.4.
      throw error instanceof Error ? error : new Error(String(error), { cause: error });
    },
  );
  return () => httpClient.interceptors.response.eject(id);
}
