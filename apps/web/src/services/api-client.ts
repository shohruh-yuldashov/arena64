import { env } from "@/lib/env";
import { ApiError, type ApiErrorBody } from "@/types/api";

interface RequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
}

/**
 * Every request carries its own correlation id, echoed by
 * `apps/api/app/common/middleware.py`'s `CorrelationIdMiddleware` — so a
 * request that fails can be traced through backend logs from the moment
 * this client sent it, before any session or auth concept exists to
 * identify who sent it.
 */
function buildHeaders(headers?: HeadersInit): Headers {
  const merged = new Headers(headers);
  merged.set("Content-Type", "application/json");
  merged.set("X-Correlation-Id", crypto.randomUUID());
  return merged;
}

async function safeParseJson<T>(response: Response): Promise<T | null> {
  try {
    return (await response.json()) as T;
  } catch {
    return null;
  }
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, headers, ...rest } = options;

  const response = await fetch(`${env.NEXT_PUBLIC_API_URL}${path}`, {
    ...rest,
    headers: buildHeaders(headers),
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (!response.ok) {
    const errorBody = await safeParseJson<ApiErrorBody>(response);
    throw new ApiError(response.status, errorBody);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

/**
 * The one HTTP client for the platform's API. Deliberately generic — path
 * and method in, typed body out. Endpoint-specific functions
 * (`getProfile()`, `submitMove()`, ...) belong beside the feature that
 * owns them, once one exists (`src/features/<name>/`), not here: this file
 * has no business knowledge and should never acquire any.
 *
 * Authentication is out of scope for this foundation (the task's
 * "Authentication Provider (placeholder only)"). The place to add a
 * credential — a header, a cookie — is `buildHeaders` above, the moment
 * `auth` exists; nothing else in this module should need to change.
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
