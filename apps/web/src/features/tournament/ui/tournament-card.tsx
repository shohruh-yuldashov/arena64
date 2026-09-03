import { Link } from "@tanstack/react-router";

import type { Tournament } from "@/features/tournament/api";
import { formatKey, speedClassKey, variantKey } from "@/features/tournament/ui/labels";
import { TournamentStatusBadge } from "@/features/tournament/ui/status-badge";
import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { formatDate } from "@/shared/lib/format";

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
          <span>{t(speedClassKey(tournament.speed_class))}</span>
        </div>

        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
          <span className="tabular-nums">
            {t("tournament.field.entrants")}:{" "}
            {t("tournament.entrantsOf", {
              count: tournament.entrant_count,
              capacity: tournament.capacity,
            })}
          </span>

          {/* Only where the backend supplies it. A tournament an operator
              closes by hand has no deadline, and §5 forbids inventing one. */}
          {tournament.registration_deadline !== null &&
            tournament.registration_deadline !== undefined && (
              <span>
                {t(open ? "tournament.deadlineOpen" : "tournament.deadlineClosed", {
                  when: formatDate(tournament.registration_deadline, locale) ?? "",
                })}
              </span>
            )}

          {tournament.current_round !== null && tournament.current_round !== undefined && (
            <span>{t("tournament.currentRound", { round: tournament.current_round })}</span>
          )}

          <span className="text-muted-foreground">
            {t("tournament.field.created")}: {formatDate(tournament.created_at, locale)}
          </span>
        </div>
      </Link>
    </li>
  );
}
