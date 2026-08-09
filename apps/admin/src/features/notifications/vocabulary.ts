import type { TranslationKey } from "@/shared/i18n";

/**
 * The delivery vocabulary, localised — A64-024.7 §23.
 *
 * The server stores and sends its own bounded labels; the console turns them
 * into sentences an operator reads, in uz, ru or en. That is the same split
 * every other admin surface makes, and it is what keeps the platform's
 * languages out of the API.
 *
 * ## The one phrase that matters most
 *
 * `sent` / `delivered` is rendered "accepted by the push service", never
 * "delivered". Web Push tells this platform that a push service took the
 * request, and nothing downstream ever reports that a device showed
 * anything — so a green "Delivered" badge would be a claim the system
 * cannot support, and an operator would use it to close an investigation
 * that should stay open.
 *
 * ## Unknown labels keep their identifier
 *
 * Both maps are consulted with a fallback to the raw string. A delivery
 * outcome added by a later backend is readable in an older console rather
 * than blank — an operator seeing `some_new_outcome` knows something is
 * there, where an empty cell tells them nothing is.
 */

/** The list's one-word push standing, worst-device-first on the server. */
export const PUSH_SUMMARY_LABELS: Record<string, TranslationKey> = {
  none: "notifications.pushNone",
  pending: "notifications.pushPending",
  sent: "notifications.pushSent",
  skipped: "notifications.pushSkipped",
  failed: "notifications.pushFailed",
};

/** Per-device delivery status — the same four the domain defines. */
export const DELIVERY_STATUS_LABELS: Record<string, TranslationKey> = {
  pending: "notifications.pushPending",
  sent: "notifications.pushSent",
  skipped: "notifications.pushSkipped",
  failed: "notifications.pushFailed",
};

/**
 * Why a delivery ended where it did.
 *
 * Every member is a label the platform chose — never a push service's own
 * error text, which is why it is safe to render at all.
 */
export const DELIVERY_OUTCOME_LABELS: Record<string, TranslationKey> = {
  delivered: "notifications.outcomeDelivered",
  skipped_preference: "notifications.outcomeSkippedPreference",
  skipped_unsupported_type: "notifications.outcomeSkippedUnsupported",
  skipped_no_subscription: "notifications.outcomeSkippedNoSubscription",
  skipped_channel_unavailable: "notifications.outcomeSkippedChannel",
  subscription_gone: "notifications.outcomeSubscriptionGone",
  retryable_failure: "notifications.outcomeRetryable",
  permanent_failure: "notifications.outcomePermanent",
  attempts_exhausted: "notifications.outcomeExhausted",
};
