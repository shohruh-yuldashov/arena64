import axios from "axios";

import type { ApiErrorBody, ErrorCode } from "@/shared/api/types";

/**
 * Every failure this app can see, as one type.
 *
 * ## Why normalisation happens here and not at each call site
 *
 * A request can fail four ways — the API refused it, the network never
 * reached the API, the caller cancelled it, or something in this app
 * threw — and Axios represents all four as an `AxiosError` whose useful
 * fields sit in different places each time. Left unnormalised, every
 * caller re-implements `error.response?.data?.code ?? ...` and each one
 * gets a slightly different subset right.
 *
 * So `normalizeError` is the only place that reads Axios' shape, and
 * everything above it branches on `kind` and `code` — CLAUDE.md §9.5's
 * typed taxonomy, at the frontend boundary.
 */
export type ApiErrorKind =
  /** The API answered, with a status and (usually) a coded body. */
  | "http"
  /** No response arrived: offline, DNS, CORS, timeout. Retrying may work. */
  | "network"
  /** The caller aborted it — a superseded query, a closed page. Not a fault. */
  | "canceled"
  /** Not a request failure at all. A defect in this app. */
  | "unknown";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  /** `null` for every kind but `"http"`. */
  readonly status: number | null;
  /** `null` when the server produced no body in the expected shape. */
  readonly code: ErrorCode | null;
  readonly requestId: string | null;
  readonly correlationId: string | null;

  constructor(
    message: string,
    options: {
      kind: ApiErrorKind;
      status?: number | null;
      body?: ApiErrorBody | null;
      cause?: unknown;
    },
  ) {
    // `cause` is preserved on every path — CLAUDE.md §9.4. A tidier message
    // that discarded the original stack would make the one error worth
    // debugging the one impossible to debug.
    super(message, { cause: options.cause });
    this.name = "ApiError";
    this.kind = options.kind;
    this.status = options.status ?? null;
    this.code = options.body?.code ?? null;
    this.requestId = options.body?.request_id ?? null;
    this.correlationId = options.body?.correlation_id ?? null;
  }

  /** Whether retrying the identical request could plausibly succeed. */
  get isRetryable(): boolean {
    if (this.kind === "network") return true;
    if (this.kind !== "http" || this.status === null) return false;
    // 5xx and 429 only. A 4xx means the request itself was wrong, and
    // retrying it unchanged is how a client turns one failure into ten
    // (CLAUDE.md §9.10).
    return this.status >= 500 || this.status === 429;
  }
}

/** Any thrown value as an `ApiError`. Never throws, never returns null. */
export function normalizeError(error: unknown): ApiError {
  if (error instanceof ApiError) {
    return error;
  }

  if (axios.isCancel(error)) {
    return new ApiError("The request was cancelled.", { kind: "canceled", cause: error });
  }

  if (axios.isAxiosError(error)) {
    const response = error.response;
    if (response === undefined) {
      return new ApiError("The server could not be reached.", {
        kind: "network",
        cause: error,
      });
    }
    const body = asErrorBody(response.data);
    return new ApiError(body?.message ?? `Request failed with status ${response.status}.`, {
      kind: "http",
      status: response.status,
      body,
      cause: error,
    });
  }

  return new ApiError(error instanceof Error ? error.message : "Something went wrong.", {
    kind: "unknown",
    cause: error,
  });
}

/**
 * The response body, when it is the shape `app/api/exception_handlers.py`
 * produces. `null` when the failure happened before the API could produce
 * one — a proxy timeout, a crashed process — in which case the status is
 * still meaningful and the body simply is not there.
 */
function asErrorBody(value: unknown): ApiErrorBody | null {
  if (typeof value !== "object" || value === null) return null;
  const candidate = value as Record<string, unknown>;
  return typeof candidate.code === "string" && typeof candidate.message === "string"
    ? (candidate as unknown as ApiErrorBody)
    : null;
}
