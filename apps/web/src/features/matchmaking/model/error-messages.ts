import { ApiError, type ErrorCode } from "@/shared/api";
import type { TranslationKey } from "@/shared/i18n";

/**
 * Backend error codes to translation keys — A64-020.5A §21.
 *
 * ## Only codes the registry actually has
 *
 * §21 lists twelve candidate categories and `app/core/error_codes.py`
 * publishes **three** of them for this surface — `unsupported_time_control`,
 * `queue_cooldown_active` and the platform's generic `conflict`,
 * `not_found`, `validation_error` and `rate_limited`. There is no
 * `queue_ticket_already_exists`, no `match_already_resolved` and no
 * `acceptance_deadline_expired`: the backend answers all three with
 * `conflict`, deliberately, and the endpoint plus the status is what
 * distinguishes them.
 *
 * So this table has no entry for them. Three languages of strings for codes
 * the server cannot send would be dead weight, and a table with invented
 * entries is one nobody can trust — the same rule `socialErrorKey` follows.
 *
 * ## Why `conflict` is contextual and everything else is not
 *
 * A `409` from `POST /queue` means "you already hold a ticket"; a `409`
 * from `/accept` means "that offer is no longer open". Same code, different
 * sentence, and the difference is one a player acts on — so `queueErrorKey`
 * takes the surface as an argument rather than rendering one message that
 * is vague enough to cover both.
 *
 * That is the whole of the contextualisation. Nothing here branches on a
 * backend *message*: §21 forbids it, and an English string is not an API.
 */
type Surface = "queue" | "match";

const SHARED: Partial<Record<ErrorCode, TranslationKey>> = {
  unsupported_time_control: "play.errors.unsupported_time_control",
  queue_cooldown_active: "play.errors.queue_cooldown_active",
  validation_error: "play.errors.validation_error",
  rate_limited: "play.errors.rate_limited",
  permission_denied: "play.errors.permission_denied",
};

/** The two codes whose meaning depends on which endpoint refused. */
const BY_SURFACE: Record<Surface, Partial<Record<ErrorCode, TranslationKey>>> = {
  queue: {
    conflict: "play.errors.already_queued",
    not_found: "play.errors.queue_not_found",
  },
  match: {
    conflict: "play.errors.match_resolved",
    not_found: "play.errors.match_not_found",
  },
};

/** The key to render for a thrown value. Never throws. */
export function queueErrorKey(error: unknown, surface: Surface): TranslationKey {
  if (!(error instanceof ApiError)) return "play.errors.unexpected";
  if (error.kind === "network") return "play.errors.network";
  if (error.code === null) return "play.errors.unexpected";
  return BY_SURFACE[surface][error.code] ?? SHARED[error.code] ?? "play.errors.unexpected";
}

/**
 * How long a cooldown has left, in seconds, or `null`.
 *
 * Read from `Retry-After`, which the backend emits in delta-seconds beside
 * `queue_cooldown_active`. §17 forbids inventing a duration and this is why
 * it does not have to be invented: the server states one, and a client that
 * guessed would show a countdown that ends before the bar lifts.
 *
 * `null` for every other failure, including a cooldown whose header was
 * stripped by a proxy — in which case the message is shown without a
 * number, which is honest rather than wrong.
 */
export function cooldownSeconds(error: unknown): number | null {
  if (!(error instanceof ApiError)) return null;
  if (error.code !== "queue_cooldown_active") return null;
  return error.retryAfterSeconds;
}
