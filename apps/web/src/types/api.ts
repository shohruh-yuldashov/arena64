/**
 * The wire shapes of `apps/api`'s HTTP contract. Mirrors
 * `apps/api/app/core/responses.py`, `app/core/pagination.py`, and
 * `app/api/exception_handlers.py` exactly — snake_case included, where the
 * backend uses it. This is a contract with a system this app does not
 * own; renaming fields to camelCase here would just move the translation
 * problem to every call site instead of removing it.
 *
 * No shared package crosses the Python/TypeScript boundary yet, so these
 * types are kept in sync by hand. If the backend's shapes in the files
 * named above ever change, this file is the one to update alongside them
 * — a documented follow-up is generating this file from the backend's
 * OpenAPI schema once one exists, rather than a gap introduced silently.
 */

// --- error codes -----------------------------------------------------------
// Mirrors apps/api/app/core/error_codes.py's ErrorCode exactly.
export type ErrorCode =
  | "internal_error"
  | "validation_error"
  | "domain_error"
  | "authentication_failed"
  | "not_found"
  | "conflict"
  | "permission_denied"
  | "precondition_failed"
  | "rule_violation"
  | "rate_limited"
  | "infrastructure_error"
  | "transient_infrastructure_error"
  | "permanent_infrastructure_error"
  // Module-specific codes. The backend adds one only where a client must
  // behave differently and the status alone cannot say — a sign-up form
  // has to name which field was rejected so the UI knows which input to
  // focus and annotate. 409s (already taken) and 422s (invalid) both need
  // that, which is why both kinds appear here.
  | "username_already_exists"
  | "email_already_exists"
  | "invalid_username"
  | "invalid_email"
  | "weak_password"
  // Login. `invalid_credentials` is deliberately generic — it means
  // "email or password wrong" and never says which, so it cannot be used
  // to discover which addresses have accounts. The other two are only
  // ever returned to a caller who already supplied the correct password.
  | "invalid_credentials"
  | "inactive_account"
  | "account_locked";

/** Mirrors `app.api.exception_handlers.ErrorResponse`. */
export interface ApiErrorBody {
  code: ErrorCode;
  message: string;
  request_id: string | null;
  correlation_id: string | null;
}

/**
 * Thrown by `services/error-parser.ts` for any non-2xx response. Carries
 * the parsed error body when the server returned one in the expected
 * shape, and the HTTP status always.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: ErrorCode | null;
  readonly requestId: string | null;
  readonly correlationId: string | null;

  constructor(status: number, body: ApiErrorBody | null) {
    super(body?.message ?? `Request failed with status ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = body?.code ?? null;
    this.requestId = body?.request_id ?? null;
    this.correlationId = body?.correlation_id ?? null;
  }
}

// --- the standard success envelope -----------------------------------------
// Mirrors app.core.responses.ResponseMeta / ApiResponse[T].
export interface ResponseMeta {
  request_id: string | null;
  correlation_id: string | null;
}

export interface ApiResponse<T> {
  data: T;
  meta: ResponseMeta;
}

// --- pagination --------------------------------------------------------
// Mirrors app.core.pagination — see that module's docstring for when each
// form applies. Keyset (cursor) is the default; offset is the documented
// exception for small, bounded, jump-to-page listings.
export interface PageInfo {
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface OffsetPage<T> {
  items: T[];
  page: PageInfo;
}

export interface CursorPageInfo {
  next_cursor: string | null;
  has_more: boolean;
}

export interface CursorPage<T> {
  items: T[];
  page: CursorPageInfo;
}
