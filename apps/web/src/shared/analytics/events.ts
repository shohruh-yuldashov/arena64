/**
 * The four events a browser may report — A64-027.1 §38, A64-027.2 §35.
 *
 * A **typed map**, not `capture(name: string, properties: any)`. The
 * server refuses anything outside this set, so a mistyped name here would
 * be a `422` nobody sees rather than a compile error — which is the wrong
 * place to find out, because behavioural events are fire-and-forget and
 * their failures are invisible by design.
 *
 * The names and the property vocabularies mirror
 * `app/platform/analytics/registry.py` and
 * `app/modules/analytics/domain/properties.py`. They are asserted equal by
 * a test rather than generated: four events is not worth a codegen step,
 * and a generated file would still need the assertion.
 */

/** Where a registration call to action was activated — M2's dimension. */
export type CtaPlacement = "hero" | "header" | "footer" | "closing" | "tournament";

/** Mirrors the tournament status vocabulary. */
export type TournamentStatus =
  | "draft"
  | "registration_open"
  | "registration_closed"
  | "in_progress"
  | "completed"
  | "cancelled";

/**
 * Every client event, with the exact properties the server's schema
 * accepts. An extra key is a `422` — the schemas are closed — so this map
 * is the compile-time half of that contract.
 */
export interface ClientEvents {
  landing_viewed: {
    utm_source?: string;
    utm_medium?: string;
    utm_campaign?: string;
  };
  register_cta_clicked: { placement: CtaPlacement };
  public_tournament_viewed: { tournament_id: string; status: TournamentStatus };
  share_clicked: {
    surface: "tournament";
    mechanism: "share_sheet" | "clipboard";
  };
}

export type ClientEventName = keyof ClientEvents;

/** The names, at runtime, for the test that asserts them against the server. */
export const CLIENT_EVENT_NAMES: readonly ClientEventName[] = [
  "landing_viewed",
  "register_cta_clicked",
  "public_tournament_viewed",
  "share_clicked",
];
