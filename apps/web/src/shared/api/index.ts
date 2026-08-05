/**
 * `shared/api`'s published surface — the only entry point the layers above
 * import from, so an endpoint function cannot reach past `request` into
 * the Axios instance and register its own interceptor.
 */
export { onUnauthorized, withAuthorization } from "./client";
export { ApiError, type ApiErrorKind, normalizeError } from "./errors";
export {
  createQueryClient,
  QUERY_GC_TIME_MS,
  QUERY_MAX_RETRIES,
  QUERY_STALE_TIME_MS,
} from "./query-client";
export { createQueryKeys, type QueryKeyFactory } from "./query-keys";
export { api, request } from "./request";
export type {
  ApiErrorBody,
  ApiResponse,
  CursorPage,
  CursorPageInfo,
  ErrorCode,
  OffsetPage,
  PageInfo,
  ResponseMeta,
} from "./types";
