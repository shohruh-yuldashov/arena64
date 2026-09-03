import { Link, useParams } from "@tanstack/react-router";

import { isAuthenticated } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import { useBracket, useStandings, useTournament } from "@/features/tournament/model/queries";
import { BracketView } from "@/features/tournament/ui/bracket-view";
import { formatKey, speedClassKey, variantKey } from "@/features/tournament/ui/labels";
import { RegistrationPanel } from "@/features/tournament/ui/registration-panel";
import { StandingsTable } from "@/features/tournament/ui/standings-table";
import { TournamentStatusBadge } from "@/features/tournament/ui/status-badge";
import { ApiError } from "@/shared/api/errors";
import { useTranslation } from "@/shared/i18n";
import { formatDate, formatDateTime } from "@/shared/lib/format";
import { Button, Skeleton } from "@/shared/ui";

/**
 * One tournament — A64-020.6 §7, §17, §18, §22, §25.
 *
 * ## Four reads, and only the ones that can say something
 *
 * The detail, the viewer's entry, the bracket and — **only once the
 * tournament has completed** — the standings. The last is not merely
 * unpolled but unrequested before then: the endpoint answers with an empty
 * list while a tournament is being played, because standings are
 * materialised once at completion, so asking early is a request whose
 * answer is already known.
 *
 * ## No invented prose
 *
 * §7. `TournamentResponse` has no description field, so there is no
 * description here and no placeholder standing in for one. Explaining what
 * a single-elimination bracket is, or who should enter, is A64-025's work
 * and would be this phase inventing product copy.
 *
 * `created_by` is not published at all, so there is no organiser line.
 *
 * ## Layout
 *
 * One column on a phone, summary-beside-registration from `sm`, and the
 * bracket full width beneath because it is the thing that needs the width.
 * The bracket's own container is the only horizontal scroller on the page.
 */
export default function TournamentPage() {
  const { tournamentId } = useParams({ from: "/tournaments/$tournamentId" });
  const { t, locale } = useTranslation();
  const { state: session } = useSession();
  const viewerId = isAuthenticated(session) ? session.user.id : null;

  const detail = useTournament(tournamentId);
  const tournament = detail.data;

  const bracket = useBracket(tournamentId, tournament?.status);
  const standings = useStandings(tournamentId, tournament?.status);

  if (detail.isPending) {
    return (
      <section className="mx-auto flex w-full max-w-3xl flex-col gap-4 py-6">
        <span role="status" className="sr-only">
          {t("tournament.loading")}
        </span>
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-48 w-full" />
      </section>
    );
  }

  // A tournament that does not exist is a `404`, never a `403` — the
  // resource is there for everybody or absent for everybody — so this is
  // the one failure worth its own screen rather than a retry button.
  if (detail.isError) {
    const missing = detail.error instanceof ApiError && detail.error.status === 404;
    return (
      <section className="mx-auto flex w-full max-w-3xl flex-col items-start gap-3 py-10">
        <div role="alert" className="flex flex-col items-start gap-2">
          <h1 className="text-lg font-semibold">
            {t(missing ? "tournament.notFoundTitle" : "tournament.detailError")}
          </h1>
          {missing && (
            <p className="text-muted-foreground text-sm">{t("tournament.notFoundBody")}</p>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          {!missing && (
            <Button
              variant="outline"
              className="min-h-11"
              onClick={() => void detail.refetch()}
            >
              {t("common.retry")}
            </Button>
          )}
          <Button asChild variant="outline" className="min-h-11">
            <Link to="/tournaments">{t("tournament.backToList")}</Link>
          </Button>
        </div>
      </section>
    );
  }

  // Unreachable: the two branches above cover pending and error, so a
  // settled query has data. Narrowed rather than asserted, because a `!`
  // here would be a runtime crash on a shape change instead of a blank.
  if (tournament === undefined) return <></>;

  const finished = tournament.status === "completed";
  const cancelled = tournament.status === "cancelled";
  const myStanding = standings.data?.standings.find(
    (standing) => standing.player_id === viewerId,
  );

  return (
    <section className="mx-auto flex w-full max-w-4xl flex-col gap-6 py-6">
      <header className="flex flex-col gap-2">
        <Button asChild variant="ghost" className="min-h-11 self-start px-2">
          <Link to="/tournaments">{t("tournament.backToList")}</Link>
        </Button>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold">{tournament.name}</h1>
          {/* The same badge the card that linked here drew, rather than the
              same fact buried in the line below it. */}
          <TournamentStatusBadge status={tournament.status} />
        </div>
        <p className="text-muted-foreground text-sm">
          {t(formatKey(tournament.format))} ·{" "}
          {t(tournament.rated ? "tournament.field.rated" : "tournament.field.casual")}
        </p>
        {cancelled && (
          <p role="status" className="text-sm">
            {t("tournament.cancelled")}
          </p>
        )}
      </header>

      <div className="grid gap-4 sm:grid-cols-2">
        <dl className="border-border grid grid-cols-2 gap-x-4 gap-y-2 rounded-lg border p-4 text-sm">
          <Fact label={t("tournament.field.entrants")}>
            {t("tournament.entrantsOf", {
              count: tournament.entrant_count,
              capacity: tournament.capacity,
            })}
          </Fact>
          <Fact label={t("tournament.field.variant")}>{t(variantKey(tournament.variant))}</Fact>
          <Fact label={t("tournament.field.speed")}>
            {t(speedClassKey(tournament.speed_class))}
          </Fact>
          {/* Omitted rather than dashed when there is none, which is how
              `started_at` and `completed_at` below already behave. A
              tournament that has not begun has no round, and an em dash in a
              definition list reads as a value that failed to load. */}
          {tournament.current_round != null && (
            <Fact label={t("tournament.field.round")}>
              {t("tournament.currentRound", { round: tournament.current_round })}
            </Fact>
          )}
          <Fact label={t("tournament.field.deadline")}>
            {/* The server's timestamp, rendered through `Intl` — §11. No
                countdown: this client does not decide whether entries are
                open, and a ticking number that reached zero would look
                like it had. */}
            {tournament.registration_deadline != null
              ? (formatDateTime(tournament.registration_deadline, locale) ?? "—")
              : t("tournament.deadlineNone")}
          </Fact>
          <Fact label={t("tournament.field.created")}>
            {formatDate(tournament.created_at, locale) ?? "—"}
          </Fact>
          {tournament.started_at != null && (
            <Fact label={t("tournament.field.started")}>
              {formatDate(tournament.started_at, locale) ?? "—"}
            </Fact>
          )}
          {tournament.completed_at != null && (
            <Fact label={t("tournament.field.finished")}>
              {formatDate(tournament.completed_at, locale) ?? "—"}
            </Fact>
          )}
        </dl>

        {/* §17: no registration controls on a completed tournament. The
            panel is dropped entirely rather than rendered disabled — a
            finished tournament is not one you failed to enter. */}
        {!finished && !cancelled && <RegistrationPanel tournament={tournament} />}

        {finished && myStanding !== undefined && (
          <section
            aria-labelledby="your-result-heading"
            className="border-border flex flex-col gap-2 rounded-lg border p-4"
          >
            <h2 id="your-result-heading" className="text-sm font-semibold">
              {t("tournament.registration.title")}
            </h2>
            <p className="text-sm">
              {t("tournament.standings.yourRank", { rank: myStanding.final_rank })}
            </p>
          </section>
        )}
      </div>

      {finished && (
        <section aria-labelledby="standings-heading" className="flex flex-col gap-3">
          <h2 id="standings-heading" className="text-base font-semibold">
            {t("tournament.standings.title")}
          </h2>

          {standings.isPending && (
            <>
              <span role="status" className="sr-only">
                {t("tournament.standings.loading")}
              </span>
              <Skeleton className="h-32 w-full" />
            </>
          )}
          {standings.isError && (
            <div role="alert" className="flex flex-col items-start gap-2">
              <p className="text-sm">{t("tournament.standings.error")}</p>
              <Button
                variant="outline"
                className="min-h-11"
                onClick={() => void standings.refetch()}
              >
                {t("common.retry")}
              </Button>
            </div>
          )}
          {standings.data !== undefined && (
            <StandingsTable standings={standings.data} viewerId={viewerId} />
          )}
        </section>
      )}

      <section aria-labelledby="bracket-heading" className="flex flex-col gap-3">
        <h2 id="bracket-heading" className="text-base font-semibold">
          {t("tournament.bracket.title")}
        </h2>

        {bracket.isPending && (
          <>
            <span role="status" className="sr-only">
              {t("tournament.bracket.loading")}
            </span>
            <Skeleton className="h-48 w-full" />
          </>
        )}
        {bracket.isError && (
          <div role="alert" className="flex flex-col items-start gap-2">
            <p className="text-sm">{t("tournament.bracket.error")}</p>
            <Button
              variant="outline"
              className="min-h-11"
              onClick={() => void bracket.refetch()}
            >
              {t("common.retry")}
            </Button>
          </div>
        )}
        {bracket.data !== undefined && <BracketView bracket={bracket.data} />}
      </section>
    </section>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="tabular-nums">{children}</dd>
    </>
  );
}
