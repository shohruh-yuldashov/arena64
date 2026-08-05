import type { Standings, TournamentParticipant } from "@/features/tournament/api";
import { participantsById } from "@/features/tournament/model/bracket";
import { finalStatusKey } from "@/features/tournament/ui/labels";
import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";

/**
 * The final placement — A64-020.6 §16, §17, §24.
 *
 * ## Ranks are the server's, verbatim
 *
 * **Not dense.** Two players knocked out in the same round share a rank, so
 * an eight-player bracket has no fourth place. §16 forbids renumbering, and
 * the reason is that renumbering publishes a comparison nobody made: the
 * bracket never decided which of the two quarter-finalists was better, and
 * a table that printed 3 and 4 would claim it did.
 *
 * Nothing here computes a placing, a tie-break or a total from the bracket
 * either. The backend materialises standings once, at completion (§6f), and
 * a second implementation on the client is a second answer that can differ.
 *
 * ## A real table, with real headers
 *
 * §24. `<th scope="col">` is what lets a screen reader announce "Wins, 3"
 * instead of "3"; a grid of `div`s reads as a wall of numbers. The shared
 * rank is announced in words on the row that shares it, because visually it
 * is conveyed by a repeated number and that is not conveyed at all in a
 * linear read.
 */
export function StandingsTable({
  standings,
  viewerId,
}: {
  standings: Standings;
  viewerId: string | null;
}) {
  const { t } = useTranslation();
  const participants = participantsById(standings.participants ?? []);

  if (standings.standings.length === 0) {
    return <p className="text-muted-foreground text-sm">{t("tournament.standings.empty")}</p>;
  }

  // Which ranks more than one player holds — for the "tied" announcement.
  // Counted rather than compared with a neighbour, because a tie can span
  // more than two rows in a bracket with byes.
  const shared = new Set(
    standings.standings
      .map((standing) => standing.final_rank)
      .filter((rank, _index, all) => all.filter((other) => other === rank).length > 1),
  );

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <caption className="sr-only">{t("tournament.standings.title")}</caption>
        <thead>
          <tr className="border-border text-muted-foreground border-b text-left text-xs">
            <th scope="col" className="py-2 pr-3 font-medium">
              {t("tournament.standings.rank")}
            </th>
            <th scope="col" className="py-2 pr-3 font-medium">
              {t("tournament.standings.player")}
            </th>
            <th scope="col" className="py-2 pr-3 font-medium">
              {t("tournament.standings.seed")}
            </th>
            <th scope="col" className="py-2 pr-2 text-right font-medium">
              {t("tournament.standings.wins")}
            </th>
            <th scope="col" className="py-2 pr-2 text-right font-medium">
              {t("tournament.standings.losses")}
            </th>
            <th scope="col" className="py-2 pr-2 text-right font-medium">
              {t("tournament.standings.draws")}
            </th>
            <th
              scope="col"
              className="py-2 pr-3 text-right font-medium"
              title={t("tournament.standings.advancementsLong")}
            >
              {t("tournament.standings.advancements")}
            </th>
            <th scope="col" className="py-2 font-medium">
              {t("tournament.standings.finalStatus")}
            </th>
          </tr>
        </thead>
        <tbody>
          {standings.standings.map((standing) => {
            const isViewer = viewerId !== null && standing.player_id === viewerId;
            return (
              <tr
                key={standing.player_id}
                className={cn(
                  "border-border border-b last:border-b-0",
                  isViewer && "bg-accent/40",
                )}
              >
                <td className="py-2 pr-3 tabular-nums">
                  {standing.final_rank}
                  {shared.has(standing.final_rank) && (
                    <span className="sr-only">
                      {" "}
                      {t("tournament.standings.tied", { rank: standing.final_rank })}
                    </span>
                  )}
                </td>
                <td className="max-w-[12rem] truncate py-2 pr-3">
                  {nameOf(standing.player_id, participants, t("tournament.unknownPlayer"))}
                </td>
                <td className="py-2 pr-3 tabular-nums">{standing.seed_number}</td>
                <td className="py-2 pr-2 text-right tabular-nums">{standing.wins}</td>
                <td className="py-2 pr-2 text-right tabular-nums">{standing.losses}</td>
                <td className="py-2 pr-2 text-right tabular-nums">{standing.draws}</td>
                <td className="py-2 pr-3 text-right tabular-nums">
                  {standing.adjudicated_advancements}
                </td>
                <td className="py-2">{t(finalStatusKey(standing.final_status))}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function nameOf(
  playerId: string,
  participants: Map<string, TournamentParticipant>,
  fallback: string,
): string {
  const participant = participants.get(playerId);
  return participant?.display_name ?? participant?.username ?? fallback;
}
