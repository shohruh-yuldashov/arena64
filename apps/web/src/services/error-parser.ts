import { ApiError, type ApiErrorBody } from "@/types/api";

/**
 * Turns a non-2xx `Response` into a thrown `ApiError` — the one place that
 * knows how to read `apps/api`'s error body shape
 * (`app/api/exception_handlers.py`'s `ErrorResponse`), so
 * `services/api-client.ts` doesn't have to.
 */
export async function parseApiError(response: Response): Promise<ApiError> {
  const body = await safeParseErrorBody(response);
  return new ApiError(response.status, body);
}

async function safeParseErrorBody(response: Response): Promise<ApiErrorBody | null> {
  try {
    const body: unknown = await response.json();
    return isApiErrorBody(body) ? body : null;
  } catch {
    // The server failed before it could produce a JSON body at all (a
    // proxy timeout, a crashed process) — the status code is still
    // meaningful; the body just isn't there to parse.
    return null;
  }
}

function isApiErrorBody(value: unknown): value is ApiErrorBody {
  return (
    typeof value === "object" &&
    value !== null &&
    "code" in value &&
    "message" in value &&
    typeof (value as Record<string, unknown>).code === "string" &&
    typeof (value as Record<string, unknown>).message === "string"
  );
}
