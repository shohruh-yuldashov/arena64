/**
 * Platform-wide constants with no business meaning — the frontend
 * counterpart to `apps/api/app/core/constants.py`. A constant belongs
 * here only once a second consumer needs it; feature-specific constants
 * belong beside the feature that owns them (`src/features/<name>/`), not
 * here.
 */

// Mirrors apps/api/app/core/constants.py's DEFAULT_PAGE_SIZE /
// MAX_PAGE_SIZE exactly — kept in sync by hand today (no shared package
// crosses the Python/TypeScript boundary yet; see types/api.ts's note on
// the same limitation for ApiErrorBody / ErrorCode).
export const DEFAULT_PAGE_SIZE = 20;
export const MAX_PAGE_SIZE = 100;

// Read by services/interceptors.ts on every request, mirrored by
// services/response-parser.ts on every response — mirrors
// apps/api/app/core/constants.py's REQUEST_ID_HEADER / CORRELATION_ID_HEADER.
export const REQUEST_ID_HEADER = "X-Request-Id";
export const CORRELATION_ID_HEADER = "X-Correlation-Id";
