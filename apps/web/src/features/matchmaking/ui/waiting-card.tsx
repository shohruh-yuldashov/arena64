import { useEffect, useState } from "react";

import type { QueueTicket } from "@/entities/queue";
import { formatMillis } from "@/entities/time-control";
import { FormError } from "@/features/auth/ui/form-status";
import { queueErrorKey } from "@/features/matchmaking/model/error-messages";
import { useLeaveQueue } from "@/features/matchmaking/model/queries";
import { Fact } from "@/features/matchmaking/ui/fact";
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
      <CardContent className="flex flex-col gap-6 pt-6">
        {/* A64-025.5 §9. The searching state is the page now, not a line at
            the top of a table, because it is the only thing happening.

            The pulse is two rings on the brand at low opacity — CSS only,
            no library, and `motion-reduce:animate-none` so it stops for
            anybody who asked their system to stop things moving. It carries
            no information: the words beside it do, and the `role="status"`
            is what announces them. */}
        <div className="flex items-center gap-4" role="status">
          <span className="relative flex size-3 shrink-0" aria-hidden="true">
            <span className="bg-primary/40 absolute inline-flex size-full animate-ping rounded-full motion-reduce:animate-none" />
            <span className="bg-primary relative inline-flex size-3 rounded-full" />
          </span>
          <div className="flex min-w-0 flex-col">
            <span className="font-medium">{t("play.waiting.searching")}</span>
            <span className="text-muted-foreground text-sm tabular-nums">
              {t("play.waiting.elapsed", { duration: formatElapsed(elapsed, locale) })}
            </span>
          </div>
        </div>

        {/* What they are queued for, as three facts rather than a form they
            can no longer change. The clock leads because it is the choice
            that decides the game. */}
        <dl className="flex flex-wrap items-center gap-2">
          <Fact label={t("play.form.timeControl")}>
            <span className="tabular-nums">{clock ?? "—"}</span>
          </Fact>
          <Fact label={t("play.waiting.speed")}>
            {t(SPEED_LABELS[ticket.speed_class] ?? "play.speed.unknown")}
          </Fact>
          <Fact label={t("play.form.mode")}>
            {t(ticket.queue_type === "ranked" ? "play.mode.ranked" : "play.mode.casual")}
          </Fact>
          <Fact label={t("play.waiting.since")}>
            {/* Semantic `<time>`: the machine-readable instant is the
                server's, and the visible text is the reader's locale. */}
            <time dateTime={ticket.entered_at} className="tabular-nums">
              {new Intl.DateTimeFormat(locale, { timeStyle: "short" }).format(
                new Date(ticket.entered_at),
              )}
            </time>
          </Fact>
        </dl>

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

        {/* Secondary, and no confirmation: leaving a queue costs nothing
            and is instantly repeatable, so a dialog would be a second click
            protecting against no consequence. The guard that matters is
            `disabled` while the request is in flight. */}
        <Button
          variant="outline"
          className="w-full sm:w-auto sm:self-start"
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
