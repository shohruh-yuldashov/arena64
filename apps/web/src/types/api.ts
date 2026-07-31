/**
 * The wire shape of `apps/api`'s error responses — mirrors
 * `apps/api/app/api/exception_handlers.py`'s `ErrorResponse` exactly,
 * snake_case included. This is a contract with a system this app does not
 * own; renaming the fields to camelCase here would just move the
 * translation problem to every call site instead of removing it.
 */
export interface ApiErrorBody {
  code: string;
  message: string;
  request_id: string | null;
}

/**
 * Thrown by `services/api-client.ts` for any non-2xx response. Carries the
 * parsed error body when the server returned one in the expected shape,
 * and the HTTP status always — callers that care about a specific
 * `code` (e.g. `"not_found"`) can branch on it once a caller exists;
 * none does yet (CLAUDE.md §1 rule 7, no speculative generality).
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly requestId: string | null;

  constructor(status: number, body: ApiErrorBody | null) {
    super(body?.message ?? `Request failed with status ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = body?.code ?? null;
    this.requestId = body?.request_id ?? null;
  }
}
