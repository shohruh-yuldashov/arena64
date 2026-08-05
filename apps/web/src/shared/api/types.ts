/**
 * The **transport** shapes of `apps/api`'s HTTP contract — the envelope
 * every response arrives in, the error body every failure arrives in, and
 * the two pagination shapes. Mirrors `apps/api/app/core/responses.py`,
 * `app/core/pagination.py`, `app/core/error_codes.py` and
 * `app/api/exception_handlers.py`.
 *
 * ## Why these are hand-written and the DTOs are not
 *
 * Endpoint payloads — a tournament, a rating, a leaderboard page — are
 * **generated** into `./generated/schema.d.ts` from the backend's own
 * OpenAPI document (`npm run openapi:generate`). Nothing in this app may
 * re-declare one by hand; a hand-copied DTO is a contract that drifts
 * silently and is discovered by a user.
 *
 * What is here instead is the plumbing the generator cannot see: FastAPI
 * describes `ApiResponse[T]` per-endpoint, and the error body is produced
 * by an exception handler rather than by a route, so neither has a stable
 * generated name to import. Four small types, deliberately, and the rule
 * for anything larger is: generate it.
 *
 * snake_case is preserved because that is what the backend sends. Renaming
 * to camelCase here would move the translation problem to every call site
 * rather than remove it.
 */

/**
 * Mirrors `app.core.error_codes.ErrorCode`.
 *
 * A closed union rather than `string`, so a client branching on a code
 * cannot misspell one — and so a code the backend removes becomes a
 * compile error here rather than a branch that silently stops matching.
 * Regenerate by reading that enum; it is the one hand-kept list in this
 * module and `specs/frontend.md` §6 says why.
 */
export type ErrorCode =
  // Platform-wide.
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
  // Reads: replay and pagination.
  | "unsupported_engine_version"
  | "invalid_cursor"
  // Registration and sign-in.
  | "username_already_exists"
  | "email_already_exists"
  | "invalid_username"
  | "invalid_email"
  | "weak_password"
  | "invalid_credentials"
  | "inactive_account"
  | "account_locked"
  // Bearer tokens and refresh sessions. Each maps to a distinct client
  // behaviour: prompt for sign-in, silently refresh, discard and re-auth.
  | "authentication_required"
  | "invalid_token"
  | "expired_token"
  | "invalid_session"
  | "session_expired"
  // One-time links. Unknown, used and expired all arrive as one code on
  // purpose — the client offers a new link either way, and separating them
  // would say whether a token was ever real.
  | "invalid_verification_token"
  | "invalid_reset_token"
  // Social.
  | "duplicate_friend_request"
  | "opposite_friend_request_pending"
  // Profiles.
  | "avatar_too_large"
  // Matchmaking.
  | "queue_cooldown_active"
  // Reference data. A64-020.5A-pre: the chosen clock is not one the
  // platform offers — unknown or retired, deliberately indistinguishable —
  // and the client's move is the same for both: read the catalogue again.
  | "unsupported_time_control"
  // Tournaments.
  | "tournament_not_found"
  | "registration_not_open"
  | "registration_deadline_passed"
  | "tournament_full"
  | "already_registered"
  | "registration_not_found"
  | "invalid_tournament_state";

/** Mirrors `app.api.exception_handlers.ErrorResponse`. */
export interface ApiErrorBody {
  code: ErrorCode;
  message: string;
  request_id: string | null;
  correlation_id: string | null;
}

/** Mirrors `app.core.responses.ResponseMeta`. */
export interface ResponseMeta {
  request_id: string | null;
  correlation_id: string | null;
}

/** Mirrors `app.core.responses.ApiResponse[T]`. */
export interface ApiResponse<T> {
  data: T;
  meta: ResponseMeta;
}

/**
 * Keyset pagination — the platform default. The cursor is **opaque**: it
 * is sent back unread, never parsed, because its contents are the
 * ordering's implementation detail (SPEC-LEADERBOARD §7.2).
 */
export interface CursorPageInfo {
  next_cursor: string | null;
  has_more: boolean;
}

export interface CursorPage<T> {
  items: T[];
  page: CursorPageInfo;
}

/** Offset pagination — the documented exception, for small bounded lists. */
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
