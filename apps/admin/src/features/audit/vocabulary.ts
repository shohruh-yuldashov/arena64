import type { TranslationKey } from "@/shared/i18n";

/**
 * The audit vocabulary, localised — A64-024.9.
 *
 * Extracted from `pages/audit.tsx` because the dashboard's activity list is
 * the second surface to render an audit action, and two copies of this map
 * would be two answers to "what does `admin.sanction.apply` say" — with the
 * newer surface silently the more complete one.
 *
 * ## Unknown actions keep their identifier
 *
 * Both consumers look up with a fallback to the raw string. The audit trail
 * is append-only and outlives the console reading it: an action added by a
 * later backend must render as `some.new.action` rather than as a blank
 * cell, because an operator seeing an identifier knows something happened
 * and an operator seeing nothing does not.
 *
 * That fallback is also why this map is allowed to lag without being a bug —
 * but it is not a reason to leave it lagging, which is why A64-024.6's and
 * A64-024.7's actions are here.
 */
export const AUDIT_ACTION_LABELS: Record<string, TranslationKey> = {
  "admin.role.grant": "audit.actionRoleGrant",
  "admin.role.revoke": "audit.actionRoleRevoke",
  "admin.sanction.apply": "audit.actionSanctionApply",
  "admin.sanction.lift": "audit.actionSanctionLift",
  "notification.broadcast.send": "audit.actionBroadcastSend",
  "notification.delivery.retry": "audit.actionDeliveryRetry",
  "tournament.create": "audit.actionTournamentCreate",
  "tournament.registration_open": "audit.actionTournamentOpen",
  "tournament.registration_close": "audit.actionTournamentClose",
  "tournament.start": "audit.actionTournamentStart",
  "tournament.transition_refused": "audit.actionTournamentRefused",
};

/**
 * Subject types this console can link to — a **closed** map on purpose.
 *
 * An unknown subject type renders as plain text. Building a link from an
 * unrecognised type would produce a route that does not exist, and a broken
 * link in an incident review is worse than no link at all.
 */
export const AUDIT_SUBJECT_ROUTES: Record<string, string> = {
  account: "/users",
  match: "/matches",
  tournament: "/tournaments",
  notification: "/notifications",
};
