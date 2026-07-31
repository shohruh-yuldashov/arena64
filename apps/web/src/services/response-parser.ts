import type { ApiResponse } from "@/types/api";

/**
 * Validates and returns the standard `{ data, meta }` envelope — the
 * inverse of `apps/api/app/core/responses.py`'s `ApiResponse[T]`.
 * `services/api-client.ts` calls this on every successful response and
 * hands callers `.data`, not the envelope: the envelope is wire-format
 * plumbing, not something a component should have to know exists.
 *
 * Throws rather than silently guessing when the shape doesn't match — a
 * response that isn't `{data, meta}` means this client and the backend
 * have disagreed about the contract, which is a defect to surface loudly,
 * not a `T | undefined` to push onto every caller.
 */
export function parseApiResponse<T>(body: unknown): ApiResponse<T> {
  if (!isApiResponseShape<T>(body)) {
    throw new Error("Response did not match the expected {data, meta} envelope");
  }
  return body;
}

function isApiResponseShape<T>(value: unknown): value is ApiResponse<T> {
  return typeof value === "object" && value !== null && "data" in value && "meta" in value;
}
