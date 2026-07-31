import { env } from "@/lib/env";
import { parseApiError } from "@/services/error-parser";
import { runRequestInterceptors, runResponseInterceptors } from "@/services/interceptors";
import { parseApiResponse } from "@/services/response-parser";

interface RequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, ...init } = options;

  const requestInit = await runRequestInterceptors({
    ...init,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  const rawResponse = await fetch(`${env.NEXT_PUBLIC_API_URL}${path}`, requestInit);
  const response = await runResponseInterceptors(rawResponse);

  if (!response.ok) {
    throw await parseApiError(response);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const body_: unknown = await response.json();
  return parseApiResponse<T>(body_).data;
}

/**
 * The one HTTP client for the platform's API. Deliberately generic — path
 * and method in, typed data out (already unwrapped from the standard
 * `{data, meta}` envelope by `services/response-parser.ts`, and already
 * translated to a thrown `ApiError` on failure by
 * `services/error-parser.ts`). Endpoint-specific functions
 * (`getProfile()`, `submitMove()`, ...) belong beside the feature that
 * owns them, once one exists (`src/features/<name>/`), not here: this file
 * has no business knowledge and should never acquire any.
 *
 * Authentication is out of scope for this foundation (the task's
 * "Authentication Provider (placeholder only)"). The place to add a
 * credential — a header, a cookie — is a new entry in
 * `services/interceptors.ts`'s `requestInterceptors`, the moment `auth`
 * exists; nothing else in this module should need to change.
 */
export const apiClient = {
  get: <T>(path: string, options?: RequestOptions) =>
    request<T>(path, { ...options, method: "GET" }),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: "POST", body }),
  put: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: "PUT", body }),
  patch: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: "PATCH", body }),
  delete: <T>(path: string, options?: RequestOptions) =>
    request<T>(path, { ...options, method: "DELETE" }),
};
