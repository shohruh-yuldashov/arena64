import { ApiError } from "@/shared/api/errors";
import type { TranslationKey } from "@/shared/i18n";

/**
 * A registration failure as something a player can read — A64-020.6 §21.
 *
 * ## Codes, never messages
 *
 * Every branch is on `ApiError.code`, which is the backend's own
 * `ErrorCode` enum and is type-checked against it. Branching on the English
 * message would break the moment the wording improves and would be
 * untranslatable by construction — the server's message is written for the
 * request log, not for this screen.
 *
 * ## Only codes these two endpoints can actually produce
 *
 * Read off `app/modules/tournament/application/ports.py`, where each
 * refusal is a typed exception with a `default_code`:
 *
 *     registration_not_open          registration has not opened, or closed
 *     registration_deadline_passed   the deadline went by
 *     tournament_full                the field filled
 *     already_registered             a second entry — a conflict, not a repeat
 *     registration_not_found         nothing to withdraw
 *     tournament_not_found           no such tournament
 *
 * Anything else falls through to one general sentence. A speculative branch
 * for a code the endpoint cannot raise is a branch nothing will ever
 * execute and nothing will ever notice is wrong.
 *
 * ## Nothing internal reaches the screen
 *
 * The returned value is a translation key, so a constraint name, a SQL
 * fragment or an exception class has no path to the DOM even if one
 * appeared in `message` (§21, §27).
 */
export function registrationErrorKey(error: unknown): TranslationKey {
  if (!(error instanceof ApiError)) return "tournament.error.unexpected";

  if (error.kind === "network") return "tournament.error.network";

  switch (error.code) {
    case "registration_not_open":
      return "tournament.error.registrationClosed";
    case "registration_deadline_passed":
      return "tournament.error.deadlinePassed";
    case "tournament_full":
      return "tournament.error.full";
    case "already_registered":
      return "tournament.error.alreadyRegistered";
    case "registration_not_found":
      return "tournament.error.notRegistered";
    case "tournament_not_found":
      return "tournament.error.notFound";
    case "rate_limited":
      return "tournament.error.rateLimited";
    case "permission_denied":
      return "tournament.error.permissionDenied";
    default:
      return "tournament.error.unexpected";
  }
}

/**
 * Whether a failure leaves the server's state genuinely unknown — §9.
 *
 * A network fault after the request left this machine is indistinguishable
 * from one before it: the entry may or may not exist. A `409` is not
 * ambiguous at all — the server answered, and it said no.
 *
 * The distinction matters because the response to ambiguity is to **re-read**
 * rather than to press the button again, which is what §9 forbids.
 */
export function isAmbiguousFailure(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  return error.kind === "network" || error.kind === "unknown" || (error.status ?? 0) >= 500;
}
