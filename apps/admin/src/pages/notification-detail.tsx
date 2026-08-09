import { Link, useParams } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import {
  DELIVERY_OUTCOME_LABELS,
  DELIVERY_STATUS_LABELS,
} from "@/features/notifications/vocabulary";
import {
  type AdminNotificationDetail,
  type AdminPushDeliveryView,
  fetchNotification,
  retryNotificationDelivery,
} from "@/shared/api/client";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { ErrorNotice } from "@/shared/ui/error-notice";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";

/**
 * One notification and every device it was owed to — A64-024.7 §19.
 *
 * **The retry lives here, not in the list.** §19 asks for it, and the reason
 * is the same one the moderation actions gave: a control on a table row is
 * one applied to whichever row was under the cursor, and this one queues
 * real work against a real person's device.
 *
 * ## Only real data is rendered
 *
 * No provider JSON, no payload, no push endpoint, no keys — the API has no
 * field for any of them. A device is three timestamps and an opaque id,
 * which is what answers "is this device still real".
 *
 * ## The action is offered where the server says it is
 *
 * `can_retry` comes from the server, which computes it from the same rule
 * its guarded `UPDATE` enforces. The button is therefore never the safety
 * boundary — a hand-made request against an ineligible delivery gets the
 * same `409` — it is only what stops an operator asking for something that
 * cannot happen.
 */
export function NotificationDetailPage() {
  const { t, locale } = useTranslation();
  const { notificationId } = useParams({ strict: false }) as { notificationId: string };

  const [detail, setDetail] = useState<AdminNotificationDetail | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [pending, setPending] = useState<AdminPushDeliveryView | null>(null);
  const [busy, setBusy] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [notice, setNotice] = useState<TranslationKey | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setState("loading");

    void fetchNotification(notificationId, controller.signal).then((outcome) => {
      if (controller.signal.aborted) return;
      if (outcome.status === "ok") {
        setDetail(outcome.value);
        setState("ready");
        return;
      }
      setState("error");
    });

    return () => controller.abort();
  }, [notificationId]);

  /**
   * Folds the server's answer back into the page.
   *
   * The response **is** the delivery's new state, so nothing is guessed —
   * and in particular the console does not claim the push succeeded. All
   * the server says is that the row is queued again, which is all that
   * happened.
   */
  const applyRetry = (delivery: AdminPushDeliveryView) => {
    setDetail((current) =>
      current === null
        ? current
        : {
            ...current,
            deliveries: current.deliveries.map((row) =>
              row.subscription_id === delivery.subscription_id ? delivery : row,
            ),
          },
    );
    setNotice("notifications.retryDone");
  };

  const confirmRetry = async () => {
    if (pending === null) return;
    setBusy(true);
    setDialogError(null);
    const outcome = await retryNotificationDelivery(notificationId, pending.subscription_id);
    setBusy(false);

    if (outcome.status === "ok") {
      applyRetry(outcome.value);
      setPending(null);
      return;
    }
    setDialogError(
      t(
        outcome.status === "refused"
          ? "notifications.retryRefused"
          : "notifications.retryFailed",
      ),
    );
  };

  const when = (value: string | null) =>
    value === null ? t("notifications.never") : new Date(value).toLocaleString(locale);

  const labelled = (map: Record<string, TranslationKey>, value: string | null) => {
    if (value === null) return t("notifications.never");
    const key = map[value];
    return key === undefined ? value : t(key);
  };

  return (
    <>
      <p>
        <Link to="/notifications">{t("notifications.back")}</Link>
      </p>

      {state === "loading" && <p role="status">{t("notifications.loading")}</p>}
      {state === "error" && <ErrorNotice message={t("notifications.error")} />}

      {state === "ready" && detail !== null && (
        <>
          <h2>{detail.type}</h2>

          <section>
            <h3>{t("notifications.sectionOverview")}</h3>
            <dl className="facts">
              <dt>{t("notifications.colRecipient")}</dt>
              <dd>
                <Link to="/users/$userId" params={{ userId: detail.recipient_id }}>
                  {detail.recipient_username ?? detail.recipient_id}
                </Link>
              </dd>
              <dt>{t("notifications.colCategory")}</dt>
              <dd>{detail.category}</dd>
              <dt>{t("notifications.colInApp")}</dt>
              <dd>
                {t(detail.read_at === null ? "notifications.unread" : "notifications.read")}
              </dd>
              <dt>{t("notifications.createdAt")}</dt>
              <dd>{when(detail.created_at)}</dd>
              <dt>{t("notifications.notificationId")}</dt>
              <dd>
                <code>{detail.id}</code>
              </dd>
            </dl>
          </section>

          <section>
            <h3>{t("notifications.sectionSource")}</h3>
            <dl className="facts">
              <dt>{t("notifications.sourceEvent")}</dt>
              <dd>
                <code>{detail.source_event_id}</code>
              </dd>
              <dt>{t("notifications.targetType")}</dt>
              <dd>{detail.target_type}</dd>
              <dt>{t("notifications.targetRef")}</dt>
              {/* Rendered as text, never as an href. §21: a stored target
                  is an internal identifier, and turning one into a link the
                  console follows would be a mapping this page must not
                  invent. */}
              <dd>{detail.target_ref ?? t("notifications.never")}</dd>
              <dt>{t("notifications.pushCapable")}</dt>
              <dd>
                {t(
                  detail.push_capable
                    ? "notifications.pushCapableYes"
                    : "notifications.pushCapableNo",
                )}
              </dd>
            </dl>
          </section>

          <section>
            <h3>{t("notifications.sectionDeliveries")}</h3>

            {detail.deliveries.length === 0 ? (
              <>
                <p role="status">{t("notifications.noDeliveries")}</p>
                <p className="muted">{t("notifications.noDeliveriesHint")}</p>
              </>
            ) : (
              <>
                <table className="users-table">
                  <thead>
                    <tr>
                      <th scope="col">{t("notifications.device")}</th>
                      <th scope="col">{t("notifications.status")}</th>
                      <th scope="col">{t("notifications.outcome")}</th>
                      <th scope="col">{t("notifications.attempts")}</th>
                      <th scope="col">{t("notifications.lastAttempt")}</th>
                      <th scope="col">{t("notifications.deviceLastSeen")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.deliveries.map((delivery) => (
                      <tr key={delivery.subscription_id}>
                        <td>
                          <code>{delivery.subscription_id.slice(0, 8)}</code>
                        </td>
                        <td>{labelled(DELIVERY_STATUS_LABELS, delivery.status)}</td>
                        <td>{labelled(DELIVERY_OUTCOME_LABELS, delivery.outcome)}</td>
                        <td>{delivery.attempt_count}</td>
                        <td>{when(delivery.last_attempt_at)}</td>
                        <td>{when(delivery.device_last_seen_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>

                <ul className="users-cards">
                  {detail.deliveries.map((delivery) => (
                    <li key={delivery.subscription_id}>
                      <span>
                        <code>{delivery.subscription_id.slice(0, 8)}</code> ·{" "}
                        {labelled(DELIVERY_STATUS_LABELS, delivery.status)}
                      </span>
                      <span>{labelled(DELIVERY_OUTCOME_LABELS, delivery.outcome)}</span>
                      <span className="muted">
                        {t("notifications.attempts")}: {delivery.attempt_count} ·{" "}
                        {when(delivery.last_attempt_at)}
                      </span>
                    </li>
                  ))}
                </ul>

                <ul className="delivery-actions">
                  {detail.deliveries.map((delivery) => (
                    <li key={delivery.subscription_id}>
                      <code>{delivery.subscription_id.slice(0, 8)}</code>{" "}
                      {delivery.can_retry ? (
                        <button
                          type="button"
                          className="action"
                          onClick={() => {
                            setDialogError(null);
                            setPending(delivery);
                          }}
                        >
                          {t("notifications.retry")}
                        </button>
                      ) : (
                        <span className="muted">{t("notifications.retryUnavailable")}</span>
                      )}
                    </li>
                  ))}
                </ul>
              </>
            )}

            {notice !== null && (
              <p role="status" className="notice">
                {t(notice)}
              </p>
            )}
          </section>

          <ConfirmDialog
            open={pending !== null}
            title={t("notifications.retryTitle")}
            description={t("notifications.retryBody", {
              type: detail.type,
              recipient: detail.recipient_username ?? detail.recipient_id,
            })}
            confirmLabel={t("notifications.retry")}
            busy={busy}
            error={dialogError}
            onCancel={() => setPending(null)}
            onConfirm={() => void confirmRetry()}
          >
            <p className="muted">{t("notifications.retryConsequence")}</p>
          </ConfirmDialog>
        </>
      )}
    </>
  );
}
