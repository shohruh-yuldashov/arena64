import type { AxiosRequestConfig } from "axios";

import { httpClient } from "@/shared/api/client";
import { normalizeError } from "@/shared/api/errors";
import type { ApiResponse } from "@/shared/api/types";

/**
 * The one way this app talks to the API.
 *
 * Two jobs, both of which every caller would otherwise repeat:
 *
 *   1. **Unwrap the envelope.** The backend answers `{data, meta}` on every
 *      success (`app/core/responses.py`). Callers want `data`; `meta` is
 *      wire plumbing and a component should not have to know it exists.
 *   2. **Normalise the failure.** Anything thrown leaves here as an
 *      `ApiError`, so a caller branches on `kind`/`code` rather than on
 *      Axios' internals.
 *
 * Endpoint functions (`getTournaments()`, `submitMove()`) belong beside
 * the feature that owns them, built on this. Nothing business-shaped
 * belongs in this file, now or later.
 *
 * `TData` is supplied by the caller from `./generated/schema.d.ts` — never
 * by a hand-written interface. See `types.ts` on why.
 */
export async function request<TData>(config: AxiosRequestConfig): Promise<TData> {
  try {
    const response = await httpClient.request<ApiResponse<TData>>(config);
    return unwrap(response.data);
  } catch (error) {
    throw normalizeError(error);
  }
}

export const api = {
  get: <TData>(url: string, config?: AxiosRequestConfig) =>
    request<TData>({ ...config, url, method: "GET" }),
  post: <TData>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
    request<TData>({ ...config, url, method: "POST", data }),
  put: <TData>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
    request<TData>({ ...config, url, method: "PUT", data }),
  patch: <TData>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
    request<TData>({ ...config, url, method: "PATCH", data }),
  delete: <TData>(url: string, config?: AxiosRequestConfig) =>
    request<TData>({ ...config, url, method: "DELETE" }),
};

/**
 * Throws rather than guessing when the body is not the envelope.
 *
 * A response that is not `{data, meta}` means this client and the backend
 * have disagreed about the contract, which is a defect to surface loudly —
 * not a `T | undefined` to push onto every caller, where it would be
 * rendered as an empty state and reported as "the page is blank sometimes".
 *
 * `204 No Content` is the one legitimate exception: there is no envelope
 * because there is no content, and `undefined` is the honest answer.
 */
function unwrap<TData>(body: ApiResponse<TData> | "" | null): TData {
  if (body === "" || body === null || body === undefined) {
    return undefined as TData;
  }
  if (typeof body !== "object" || !("data" in body)) {
    throw new Error("The response did not match the expected {data, meta} envelope.");
  }
  return body.data;
}
