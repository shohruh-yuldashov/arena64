import { useEffect, useState } from "react";

import type { QueueTicket } from "@/entities/queue";
import { formatMillis } from "@/entities/time-control";
import { FormError } from "@/features/auth/ui/form-status";
import { queueErrorKey } from "@/features/matchmaking/model/error-messages";
import { useLeaveQueue } from "@/features/matchmaking/model/queries";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { type DeliveryMode, deliveryMode, useConnectionStatus } from "@/shared/realtime";
import { Button, Card, CardContent, Spinner } from "@/shared/ui";

/**
 * "Searching for an opponent" — A64-020.5A §12, §13.
 *
 * ## What it says, and what it refuses to say
 *
 * The pool the player actually chose, the instant they entered it, and how
 * long that has been. Nothing else, and the omissions are the point:
 *
 *   - **no estimated wait.** The backend supplies none, and one computed
 *     here would be a number invented from nothing — the sort a player
 *     remembers and holds against you.
 *   - **no queue position and no player count.** `QueueTicketResponse`
 *     carries `waiting`, and it is deliberately not rendered: it is a
 *     reading of one pool at one instant, it is not a position in a line,
 *     and showing "3 waiting" beside a wait that keeps growing invites
 *     exactly the wrong inference.
 *
 * ## Elapsed time is presentation, and is announced sparingly
 *
 * `entered_at` is the fact; the elapsed seconds are arithmetic against the
 * local clock and decide nothing. The region is `aria-live="polite"` but
 * the **counter is not inside it** — a live region containing a number that
 * changes every second is a screen reader saying a number every second.
 * What is announced is the *status*, which changes when the state does.
 */
/** What each degraded mode means for a queued player — §17. */
const DELIVERY_LABELS: Record<DeliveryMode, TranslationKey> = {
  realtime: "play.waiting.delivery.realtime",
  reconnecting: "play.waiting.delivery.reconnecting",
  fallback_polling: "play.waiting.delivery.fallback",
  offline: "play.waiting.delivery.offline",
};

export function WaitingCard({
  ticket,
  disabled = false,
}: {
  ticket: QueueTicket;
  disabled?: boolean;
}) {
  const { t, locale } = useTranslation();
  const leave = useLeaveQueue();
  const [failure, setFailure] = useState<TranslationKey | null>(null);
  const mode = deliveryMode(useConnectionStatus());
  const elapsed = useElapsedSeconds(ticket.entered_at);

  const clock = formatMillis(ticket.base_time_ms, ticket.increment_ms, locale);

  const onCancel = async () => {
    setFailure(null);
    try {
      await leave.mutateAsync();
    } catch (error) {
      // §13: a failed cancel is not necessarily a failure to cancel. The
      // mutation reconciles both reads regardless, so what this message
      // says is "we could not confirm it" rather than "you are still
      // queued" — which the refetch may be about to contradict.
      setFailure(queueErrorKey(error, "queue"));
    }
  };

  return (
    <Card>
      <CardContent className="flex flex-col gap-4 pt-6">
        <div className="flex items-center gap-3" role="status">
          <Spinner label={t("play.waiting.searching")} />
          <span className="text-sm font-medium">{t("play.waiting.searching")}</span>
        </div>

        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
          <Row label={t("play.form.mode")}>
            {t(ticket.queue_type === "ranked" ? "play.mode.ranked" : "play.mode.casual")}
          </Row>
          <Row label={t("play.form.timeControl")}>
            <span className="tabular-nums">{clock ?? "—"}</span>
          </Row>
          <Row label={t("play.waiting.speed")}>
            {t(SPEED_LABELS[ticket.speed_class] ?? "play.speed.unknown")}
          </Row>
          <Row label={t("play.waiting.since")}>
            {/* Semantic `<time>`: the machine-readable instant is the
                server's, and the visible text is the reader's locale. */}
            <time dateTime={ticket.entered_at} className="tabular-nums">
              {new Intl.DateTimeFormat(locale, { timeStyle: "short" }).format(
                new Date(ticket.entered_at),
              )}
            </time>
          </Row>
        </dl>

        <p className="text-muted-foreground text-sm">
          {t("play.waiting.elapsed", { duration: formatElapsed(elapsed, locale) })}
        </p>

        {/* A64-020.5D §17. Shown **only when degraded** — a "connected"
            banner during normal operation is noise, and this line exists to
            explain why a pairing might take a moment longer than usual, not
            to report that the socket is fine. */}
        {mode !== "realtime" && (
          <p role="status" className="text-muted-foreground text-sm">
            {t(DELIVERY_LABELS[mode])}
          </p>
        )}

        {failure !== null && <FormError messageKey={failure} />}

        <Button
          variant="outline"
          className="min-h-11"
          disabled={leave.isPending || disabled}
          onClick={() => void onCancel()}
        >
          {leave.isPending ? (
            <Spinner label={t("play.waiting.cancelling")} />
          ) : (
            t("play.waiting.cancel")
          )}
        </Button>
      </CardContent>
    </Card>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-medium">{children}</dd>
    </>
  );
}

const SPEED_LABELS: Partial<Record<QueueTicket["speed_class"], TranslationKey>> = {
  bullet: "play.speed.bullet",
  blitz: "play.speed.blitz",
  rapid: "play.speed.rapid",
  classical: "play.speed.classical",
  correspondence: "play.speed.correspondence",
};

/**
 * How long since `since`, in whole seconds, ticking.
 *
 * A second is the right resolution here and no scheduling cleverness is
 * warranted: unlike the acceptance countdown, nothing acts on this number
 * and drifting by a few hundred milliseconds over ten minutes is invisible.
 */
function useElapsedSeconds(since: string): number {
  const start = Date.parse(since);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  return Math.max(0, Math.floor((now - start) / 1000));
}

/** `m:ss`, in the reader's numbering system. */
function formatElapsed(seconds: number, locale: string): string {
  const format = new Intl.NumberFormat(locale, { minimumIntegerDigits: 2 });
  const minutes = new Intl.NumberFormat(locale).format(Math.floor(seconds / 60));
  return `${minutes}:${format.format(seconds % 60)}`;
}
