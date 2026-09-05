import { useTranslation } from "@/shared/i18n";

/**
 * The analytics page's visualisations — A64-027A.4 §9, §10, §14, §17.
 *
 * ## No chart library, and the arithmetic here is not a metric
 *
 * `apps/admin`'s dependency list is React and a router. The four shapes this
 * page needs — a funnel, a cohort grid, a distribution and a flow — are a
 * width, a background and a `<table>`. `recharts` is ~100 kB and `chart.js`
 * ~70 kB against a 447 kB bundle: a fifteen-percent increase for what CSS
 * already does, plus a canvas an operator cannot select text out of.
 *
 * The only arithmetic below is `value / max` for a bar's width and
 * `rate * 100` for a cell's tint. Neither is a product metric: every rate
 * rendered here is the one the backend computed and returned, and A64-027.6
 * §15's rule holds unchanged — **this page renders; it does not compute.**
 *
 * ## Nothing is available only by hovering
 *
 * §19 and §42. Every figure a bar encodes is also written beside it as text,
 * every colour is redundant with a number, and the cohort grid is a real
 * `<table>` with real headers. A chart whose data lives in a tooltip is a
 * chart half the readers cannot use.
 */

/**
 * One stage of a funnel.
 *
 * The width is `subjects / first`, so the bars are proportional to the
 * *population* rather than to each other — which is what makes a funnel a
 * funnel. When the first stage is zero there is no proportion to draw and
 * the bar is omitted rather than rendered full.
 */
export function FunnelStage({
  label,
  subjects,
  first,
  fromPrevious,
  fromStart,
  dropOff,
  locale,
}: {
  label: string;
  subjects: number;
  first: number;
  fromPrevious: string | null;
  fromStart: string | null;
  dropOff: number;
  locale: string;
}) {
  const { t } = useTranslation();
  const share = first > 0 ? Math.max(subjects / first, 0) : 0;
  const count = (value: number) => new Intl.NumberFormat(locale).format(value);

  return (
    <div className="funnel__stage">
      <div className="funnel__head">
        <span className="funnel__label">{label}</span>
        <span className="funnel__count">{count(subjects)}</span>
      </div>

      <div className="funnel__track">
        <span
          className="funnel__fill"
          style={{ inlineSize: `${String(share * 100)}%` }}
          aria-hidden="true"
        />
      </div>

      {/* The three rates the backend computed, as text. A bar cannot say
          "from previous" and a reader should not have to infer it. */}
      <div className="funnel__rates">
        {fromPrevious !== null && (
          <span>
            <span className="muted">{t("analytics.fromPrevious")}</span> {fromPrevious}
          </span>
        )}
        {fromStart !== null && (
          <span>
            <span className="muted">{t("analytics.fromStart")}</span> {fromStart}
          </span>
        )}
        {dropOff > 0 && (
          <span className="funnel__drop">
            <span className="muted">{t("analytics.dropOff")}</span> {count(dropOff)}
          </span>
        )}
      </div>
    </div>
  );
}

/**
 * A retention cell — A64-027A.4 §14.
 *
 * The tint is the rate; the number is always written. `null` is a *distinct
 * state*, not a low value: it means the day has not arrived, and colouring
 * it as zero would show a cohort that churned rather than one that has not
 * been measured. So an unmeasured cell carries no tint at all, a dash, and
 * an accessible explanation.
 */
export function CohortCell({ rate, label }: { rate: number | null; label: string | null }) {
  const { t } = useTranslation();

  if (rate === null || label === null) {
    return (
      <td className="cohort__cell" data-unmeasured="true">
        <span aria-hidden="true">{t("analytics.unavailable")}</span>
        <span className="sr-only">{t("analytics.unavailableHint")}</span>
      </td>
    );
  }

  return (
    <td className="cohort__cell">
      {/* The tint sits behind the figure rather than replacing it, so the
          value is legible at every intensity and in forced-colours mode.
          
          The floor is a **legibility** device, not a value: a measured 2%
          and an unmeasured cell would otherwise be the same untinted
          rectangle, and those mean opposite things. It stays monotonic in
          the rate, and the number beside it is the authority. */}
      <span
        className="cohort__tint"
        style={{ opacity: 0.1 + Math.min(Math.max(rate, 0), 1) * 0.55 }}
        aria-hidden="true"
      />
      <span className="cohort__value">{label}</span>
    </td>
  );
}

/**
 * One bar of a distribution — §17.
 *
 * `max` is the largest count in the set, so the bars compare to each other
 * rather than to a total nobody returned. The label and the count are text;
 * the bar is `aria-hidden`.
 */
export function DistributionBar({
  label,
  value,
  max,
  display,
  tone,
}: {
  label: string;
  value: number;
  max: number;
  display: string;
  tone?: "primary" | "success" | "warning" | "danger";
}) {
  const share = max > 0 ? Math.max(value / max, 0) : 0;

  return (
    <div className="dist">
      <span className="dist__label">{label}</span>
      <span className="dist__track" aria-hidden="true">
        <span
          className="dist__fill"
          data-tone={tone}
          style={{ inlineSize: `${String(share * 100)}%` }}
        />
      </span>
      <span className="dist__value">{display}</span>
    </div>
  );
}

/**
 * The queue's shape, as a sequence — §15.
 *
 * Three counts the backend returns, drawn in the order a ticket travels
 * through them. The **rates between them are the server's**, not a division
 * done here: `match_found_rate` and `offer_acceptance` arrive computed, and
 * recomputing either from the counts beside them would be a second
 * definition of a product metric.
 */
export function FlowStep({
  label,
  value,
  rate,
  last,
}: {
  label: string;
  value: string;
  /** The backend's rate into this step, already formatted. */
  rate?: string | null;
  last?: boolean;
}) {
  return (
    <>
      <div className="flow__step">
        <span className="flow__value">{value}</span>
        <span className="flow__label">{label}</span>
      </div>
      {last !== true && (
        <div className="flow__link" aria-hidden={rate === undefined || rate === null}>
          <span className="flow__arrow" aria-hidden="true">
            →
          </span>
          {rate !== undefined && rate !== null && <span className="flow__rate">{rate}</span>}
        </div>
      )}
    </>
  );
}
