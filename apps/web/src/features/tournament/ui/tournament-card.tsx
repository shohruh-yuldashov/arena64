import { Link } from "@tanstack/react-router";

import { speedClassKey } from "@/entities/time-control";
import type { Tournament } from "@/features/tournament/api";
import { formatKey, variantKey } from "@/features/tournament/ui/labels";
import { TournamentStatusBadge } from "@/features/tournament/ui/status-badge";
import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { formatDate, formatDateTime, formatRelativeTime } from "@/shared/lib/format";
import { speedAccent } from "@/shared/lib/speed-accent";

/**
 * One tournament, as the lobby lists it — A64-020.6 §5, §24, §25.
 *
 * ## Only authoritative fields
 *
 * Every value here is on `TournamentResponse`. §5 forbids inferring
 * "almost full", an estimated start, a prize or an organiser — not because
 * those would be hard, but because they would be *invented*: a card that
 * says "filling fast" is making a claim the server never made, and nobody
 * downstream can tell which parts of the card came from the API.
 *
 * `created_by` is not published at all — who opened a tournament is
 * operational — so there is no organiser line and no placeholder standing
 * in for one.
 *
 * ## The whole card is one link
 *
 * Rather than a title link plus a separate "View" button: two controls to
 * one destination gives a keyboard user two stops and a screen reader two
 * announcements of the same thing.
 */
export function TournamentCard({ tournament }: { tournament: Tournament }) {
  const { t, locale } = useTranslation();

  const open = tournament.status === "registration_open";
  const cancelled = tournament.status === "cancelled";

  const deadline = tournament.registration_deadline ?? null;
  const relativeDeadline = open ? formatRelativeTime(deadline, locale, t) : null;
  // Inside a day, and still open. `formatRelativeTime` returns null past a
  // week, so a distant deadline simply reads as its date.
  const closingSoon =
    open &&
    deadline !== null &&
    new Date(deadline).getTime() - Date.now() < 24 * 60 * 60 * 1000;

  const fillPercent =
    tournament.capacity > 0 ? (tournament.entrant_count / tournament.capacity) * 100 : 0;
  // Urgency only while somebody can still act on it. A full bar on a
  // finished tournament is a fact, not a warning, and red would be telling
  // a reader to hurry about something that ended last week.
  const full = open && tournament.entrant_count >= tournament.capacity;
  const filling = open && !full && fillPercent >= 80;

  return (
    <li>
      <Link
        to="/tournaments/$tournamentId"
        params={{ tournamentId: tournament.id }}
        className={cn(
          "border-border hover:bg-accent/40 focus-visible:ring-ring flex flex-col gap-2",
          "rounded-lg border p-4 focus-visible:ring-2 focus-visible:outline-none",
          cancelled && "opacity-70",
        )}
      >
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
          <span className="text-base font-semibold">{tournament.name}</span>
          <TournamentStatusBadge status={tournament.status} />
        </div>

        {/* Separated by middots, the way the detail page's subtitle and the
            game room's seat line already are. Four grey words with only
            spacing between them read as one phrase rather than as four
            facts. */}
        <div className="text-muted-foreground flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs [&>span:not(:last-child)]:after:ml-1.5 [&>span:not(:last-child)]:after:content-['·']">
          <span>{t(formatKey(tournament.format))}</span>
          <span>
            {t(tournament.rated ? "tournament.field.rated" : "tournament.field.casual")}
          </span>
          <span>{t(variantKey(tournament.variant))}</span>
          <span className={cn("font-medium", speedAccent(tournament.speed_class).text)}>
            {t(speedClassKey(tournament.speed_class))}
          </span>
        </div>

        {/* Capacity as a bar — A64-025.7B §25. "Entrants: 27 of 32" is
            arithmetic; a bar answers "is this nearly full?" without any.
            The numbers stay beside it, because the bar is `aria-hidden` and
            colour is never the only signal.

            `--warning` from four fifths, `--destructive` when it is full:
            the states a player acts on differently. */}
        <div className="flex flex-col gap-1.5">
          <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 text-xs">
            <span className="tabular-nums">
              {t("tournament.field.entrants")}:{" "}
              {t("tournament.entrantsOf", {
                count: tournament.entrant_count,
                capacity: tournament.capacity,
              })}
            </span>

            {/* Only where the backend supplies it. A tournament an operator
                closes by hand has no deadline, and §5 forbids inventing one.

                Relative while it is open, because a deadline is read for
                urgency; the exact instant stays on the element. Once it has
                passed the date is the useful form again — "3 days ago" for
                a closed registration answers nothing. */}
            {deadline !== null && (
              <span
                title={formatDateTime(deadline, locale) ?? ""}
                className={cn(closingSoon && "text-warning font-medium")}
              >
                {open && relativeDeadline !== null
                  ? t("tournament.deadlineOpenRelative", { when: relativeDeadline })
                  : t(open ? "tournament.deadlineOpen" : "tournament.deadlineClosed", {
                      when: formatDate(deadline, locale) ?? "",
                    })}
              </span>
            )}

            {tournament.current_round !== null && tournament.current_round !== undefined && (
              <span>{t("tournament.currentRound", { round: tournament.current_round })}</span>
            )}
          </div>

          <div
            aria-hidden="true"
            className="bg-muted h-1.5 w-full overflow-hidden rounded-full"
          >
            <div
              className={cn(
                "h-full rounded-full transition-[width]",
                full
                  ? "bg-destructive"
                  : filling
                    ? "bg-warning"
                    : open
                      ? "bg-primary"
                      : "bg-muted-foreground/40",
              )}
              style={{ width: `${Math.min(100, fillPercent).toString()}%` }}
            />
          </div>
        </div>
      </Link>
    </li>
  );
}
