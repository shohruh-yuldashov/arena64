import { useCallback, useEffect, useRef, useState } from "react";

import { type BroadcastView, fetchBroadcasts } from "@/shared/api/client";
import { useTranslation } from "@/shared/i18n";
import { DataTable } from "@/shared/ui/data-table";
import { StatusBadge, type Tone } from "@/shared/ui/status-badge";
import { EmptyState, ErrorState, InfoBanner, LoadingSkeleton } from "@/shared/ui/states";

/**
 * What was sent, and how far it got — A64-027A §20.
 *
 * ## Delivered is not audience, and the difference is not a failure
 *
 * A broadcast to 12,000 accounts that wrote 11,600 rows did not fail 400
 * times: 400 people had muted the category, and the platform honoured that.
 * The table shows both figures and states the reason once, because an
 * operator who read the gap as loss would go looking for an incident that
 * did not happen.
 *
 * ## An uncounted audience is a dash
 *
 * `audience_size` is `null` until the worker has counted, and rendering
 * that as `0` would show a broadcast that reached nobody. The same rule the
 * analytics page turns on, for the same reason.
 *
 * ## No recipients, ever
 *
 * §20 and §23. The API sends how many were named and never whom, so there
 * is nothing here to render even if somebody wanted a column.
 */

const TONES: Record<string, Tone> = {
  queued: "neutral",
  sending: "info",
  completed: "success",
  failed: "danger",
};

export function BroadcastHistory({ reloadToken }: { reloadToken: number }) {
  const { t, locale } = useTranslation();
  const [items, setItems] = useState<BroadcastView[] | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const controller = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setState((current) => (current === "ready" ? current : "loading"));

    const outcome = await fetchBroadcasts(next.signal);
    if (next.signal.aborted) return;

    if (outcome.status === "ok") {
      setItems(outcome.value.items);
      setState("ready");
      return;
    }
    setState("error");
  }, []);

  // `reloadToken` changes when a send succeeds, so the history a composer
  // just added to is the history the operator sees — without polling, which
  // this console does not do anywhere.
  useEffect(() => {
    void load();
    return () => controller.current?.abort();
  }, [load, reloadToken]);

  if (state === "loading") return <LoadingSkeleton rows={4} />;
  if (state === "error") {
    return (
      <ErrorState
        title={t("broadcast.errorUnavailable")}
        onRetry={() => {
          void load();
        }}
      />
    );
  }
  if (items === null || items.length === 0) {
    return <EmptyState icon="notifications" title={t("broadcast.historyEmpty")} />;
  }

  const when = (value: string) => new Date(value).toLocaleString(locale);
  const count = (value: number) => new Intl.NumberFormat(locale).format(value);

  return (
    <>
      <InfoBanner tone="info">{t("broadcast.suppressedHint")}</InfoBanner>
      <DataTable caption={t("broadcast.tabHistory")} minWidth="46rem">
        <thead>
          <tr>
            <th scope="col">{t("broadcast.colTitle")}</th>
            <th scope="col">{t("broadcast.colAudience")}</th>
            <th scope="col">{t("broadcast.colStatus")}</th>
            <th scope="col">{t("broadcast.colDelivered")}</th>
            <th scope="col">{t("broadcast.colCreated")}</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id}>
              <th scope="row">
                <span className="cell-primary">
                  <strong>{item.title}</strong>
                  <span>{item.body}</span>
                </span>
              </th>
              <td>
                {item.audience === "specific_players"
                  ? `${t("broadcast.audienceKind.specific_players")} · ${count(item.named_recipients)}`
                  : t("broadcast.audienceKind.all_players")}
              </td>
              <td>
                <StatusBadge
                  label={t(
                    `broadcast.statusLabel.${item.status}` as "broadcast.statusLabel.queued",
                  )}
                  tone={TONES[item.status] ?? "neutral"}
                />
              </td>
              <td>
                {t("broadcast.deliveredOf", {
                  delivered: count(item.delivered),
                  // "Counting", never a zero: `null` means the worker has
                  // not counted yet, and a zero would read as a broadcast
                  // that reached nobody.
                  total:
                    item.audience_size === null
                      ? t("broadcast.deliveredUnknown")
                      : count(item.audience_size),
                })}
              </td>
              <td>
                <time dateTime={item.created_at}>{when(item.created_at)}</time>
              </td>
            </tr>
          ))}
        </tbody>
      </DataTable>
    </>
  );
}
