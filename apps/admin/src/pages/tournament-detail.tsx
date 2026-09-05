import { Link, useParams } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";

import {
  type AdminPairingView,
  type AdminTournamentDetail,
  fetchTournament,
} from "@/shared/api/client";
import { TournamentActions } from "@/features/tournaments/tournament-actions";
import { useVocab } from "@/features/vocabulary";
import { useTranslation } from "@/shared/i18n";
import { Icon } from "@/shared/ui/icon";
import { PageHeader } from "@/shared/ui/page-header";
import { StatusBadge, type Tone } from "@/shared/ui/status-badge";
import { ErrorNotice } from "@/shared/ui/error-notice";

/**
 * One tournament — A64-024.5 §17, §18.
 *
 * ## The bracket is truthful, and it is not drawn
 *
 * §11 is explicit that correctness outranks decoration. The backend
 * publishes each node's `(round_number, slot)`, and the tree follows from
 * the domain's own arithmetic: the parent is `(round + 1, slot >> 1)`.
 *
 * So this renders **round-by-round with each node stating where it feeds**,
 * computed from that arithmetic. There are no connector lines, and that is
 * the point: a line is a claim about structure, and a claim drawn in CSS
 * can be wrong in ways the data is not. What is shown here cannot
 * disagree with the bracket, because it is derived from the same rule the
 * domain uses to build it.
 *
 * It is also the accessible representation §21 asks for — the structure is
 * text, not geometry, so a screen reader reads the real tree rather than a
 * decorative one. Graphical connectors, if they are ever worth it, are
 * A64-025's polish over data that already supports them.
 *
 * Stacked by round rather than side-by-side, which is what makes a
 * 64-player bracket usable at 360px instead of unreadable dust (§22).
 *
 * Sections render only when the data exists — §17 forbids fake "coming
 * soon" panels inside a real detail page.
 */

/** Where a node's winner goes. `null` for the final, which has no parent. */
function feedsInto(
  pairing: AdminPairingView,
  rounds: number,
): { round: number; slot: number } | null {
  if (pairing.round_number >= rounds) return null;
  return { round: pairing.round_number + 1, slot: pairing.slot >> 1 };
}

/** The tones the listing uses, so a status reads identically in both. */
const STATUS_TONES: Record<string, Tone> = {
  in_progress: "success",
  registration_open: "info",
  registration_closed: "warning",
  completed: "neutral",
  draft: "neutral",
  cancelled: "danger",
};

export function TournamentDetailPage() {
  const { t, locale } = useTranslation();
  const vocab = useVocab();
  const { tournamentId } = useParams({ strict: false }) as { tournamentId: string };

  const [detail, setDetail] = useState<AdminTournamentDetail | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");

  const load = useCallback(
    (signal?: AbortSignal) =>
      fetchTournament(tournamentId, signal).then((outcome) => {
        if (signal?.aborted) return;
        if (outcome.status === "ok") {
          setDetail(outcome.value);
          setState("ready");
          return;
        }
        setState("error");
      }),
    [tournamentId],
  );

  useEffect(() => {
    const controller = new AbortController();
    setState("loading");
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const moment = (value: string | null) =>
    value === null ? t("tournaments.unknown") : new Date(value).toLocaleString(locale);

  const name = (
    person: { username: string | null; display_name: string | null; player_id: string } | null,
  ) =>
    person === null
      ? t("tournaments.unknown")
      : (person.display_name ?? person.username ?? person.player_id);

  return (
    <>
      <p>
        <Link to="/tournaments">{t("tournaments.back")}</Link>
      </p>

      {state === "loading" && <p role="status">{t("tournaments.loading")}</p>}
      {state === "error" && <ErrorNotice message={t("tournaments.error")} />}

      {state === "ready" && detail !== null && (
        <>
          {/* The tournament's standing is the header's, not a row three
              sections down: status, format and how full it is are what an
              operator opened this page to see. */}
          <PageHeader
            title={detail.tournament.name}
            description={`${vocab("tournamentFormat", detail.tournament.format)} · ${vocab(
              "variant",
              detail.tournament.variant,
            )} · ${vocab("speedClass", detail.tournament.speed_class)}`}
            actions={
              <>
                <StatusBadge
                  label={t(
                    `tournaments.statusLabel.${detail.tournament.status}` as "tournaments.statusLabel.completed",
                  )}
                  tone={STATUS_TONES[detail.tournament.status] ?? "neutral"}
                />
                <StatusBadge
                  label={t(
                    detail.tournament.rated ? "tournaments.rated" : "tournaments.casual",
                  )}
                  tone="neutral"
                />
              </>
            }
          />

          <section className="panel">
            <div className="panel__head">
              <h3>
                <Icon name="settings" size={16} />
                {t("tournamentActions.actions")}
              </h3>
            </div>
            {/* The server's status decides what is offered, and the
                aggregate decides what is allowed — a button rendered from a
                stale state still cannot do anything. After a transition the
                detail is **re-read** rather than patched locally, so the
                next set of actions comes from the same authority as the
                first. */}
            <TournamentActions
              tournamentId={detail.tournament.tournament_id}
              name={detail.tournament.name}
              status={detail.tournament.status}
              onChanged={() => void load()}
            />
          </section>

          <section>
            <h3>{t("tournaments.overview")}</h3>
            <dl className="facts">
              <dt>{t("tournaments.status")}</dt>
              <dd>
                {t(
                  `tournaments.statusLabel.${detail.tournament.status}` as "tournaments.statusLabel.completed",
                )}
              </dd>
              <dt>{t("tournaments.format")}</dt>
              <dd>{vocab("tournamentFormat", detail.tournament.format)}</dd>
              <dt>{t("tournaments.variant")}</dt>
              <dd>{vocab("variant", detail.tournament.variant)}</dd>
              <dt>{t("tournaments.speed")}</dt>
              <dd>{vocab("speedClass", detail.tournament.speed_class)}</dd>
              <dt>{t("tournaments.mode")}</dt>
              <dd>{t(detail.tournament.rated ? "tournaments.rated" : "tournaments.casual")}</dd>
              <dt>{t("tournaments.capacity")}</dt>
              <dd>
                {detail.tournament.entrant_count} / {detail.tournament.capacity}
              </dd>
              <dt>{t("tournaments.deadline")}</dt>
              <dd>{moment(detail.tournament.registration_deadline)}</dd>
              <dt>{t("tournaments.created")}</dt>
              <dd>{moment(detail.tournament.created_at)}</dd>
              <dt>{t("tournaments.started")}</dt>
              <dd>{moment(detail.tournament.started_at)}</dd>
              <dt>{t("tournaments.completed")}</dt>
              <dd>{moment(detail.tournament.completed_at)}</dd>
            </dl>
          </section>

          <section>
            <h3>{t("tournaments.entrants")}</h3>
            {detail.entrants.length === 0 ? (
              <p className="muted">{t("tournaments.noEntrants")}</p>
            ) : (
              <ul className="users-cards">
                {detail.entrants.map((entrant) => (
                  <li key={entrant.player_id}>
                    <Link to="/users/$userId" params={{ userId: entrant.player_id }}>
                      {name(entrant)}
                    </Link>
                    <span>
                      {vocab("entrantStatus", entrant.status)}
                      {entrant.seed_number !== null
                        ? ` · ${t("tournaments.seed")} ${entrant.seed_number}`
                        : ""}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section>
            <h3>{t("tournaments.rounds")}</h3>
            {detail.rounds.length === 0 ? (
              <p className="muted">{t("tournaments.noRounds")}</p>
            ) : (
              <table className="users-table">
                <thead>
                  <tr>
                    <th scope="col">{t("tournaments.round")}</th>
                    <th scope="col">{t("tournaments.status")}</th>
                    <th scope="col">{t("tournaments.pairings")}</th>
                    <th scope="col">{t("tournaments.published")}</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.rounds.map((round) => (
                    <tr key={round.round_number}>
                      <td>{round.round_number}</td>
                      <td>{round.status}</td>
                      <td>{round.pairing_count}</td>
                      <td>{moment(round.published_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          {detail.pairings.length > 0 && (
            <section>
              <h3>{t("tournaments.bracket")}</h3>
              {[...new Set(detail.pairings.map((p) => p.round_number))]
                .sort((a, b) => a - b)
                .map((roundNumber) => (
                  <div key={roundNumber}>
                    <h4>
                      {t("tournaments.round")} {roundNumber}
                    </h4>
                    <ul className="users-cards">
                      {detail.pairings
                        .filter((pairing) => pairing.round_number === roundNumber)
                        .sort((a, b) => a.slot - b.slot)
                        .map((pairing) => {
                          const parent = feedsInto(pairing, detail.rounds.length);
                          return (
                            <li key={`${pairing.round_number}-${pairing.slot}`}>
                              <span>
                                {t("tournaments.slot")} {pairing.slot}
                                {pairing.light_player_id === null &&
                                pairing.dark_player_id === null
                                  ? ` · ${t("tournaments.bye")}`
                                  : ""}
                              </span>
                              <span className="muted">
                                {/* Structure as text, never as a line — a
                                    connector drawn in CSS is a claim that
                                    can be wrong where the data is not. */}
                                {parent === null
                                  ? t("tournaments.finalRound")
                                  : t("tournaments.feedsInto", {
                                      round: parent.round,
                                      slot: parent.slot,
                                    })}
                              </span>
                              {pairing.advancement_reason !== null && (
                                <span className="muted">
                                  {t("tournaments.advancement")}: {pairing.advancement_reason}
                                </span>
                              )}
                              {pairing.match_ids.map((matchId) => (
                                <Link key={matchId} to="/matches/$matchId" params={{ matchId }}>
                                  {t("tournaments.openMatch")}
                                </Link>
                              ))}
                            </li>
                          );
                        })}
                    </ul>
                  </div>
                ))}
            </section>
          )}

          <section>
            <h3>{t("tournaments.standings")}</h3>
            {detail.standings.length === 0 ? (
              <p className="muted">{t("tournaments.noStandings")}</p>
            ) : (
              <table className="users-table">
                <thead>
                  <tr>
                    <th scope="col">{t("tournaments.rank")}</th>
                    <th scope="col">{t("tournaments.colName")}</th>
                    <th scope="col">{t("tournaments.record")}</th>
                    <th scope="col">{t("tournaments.finalStatus")}</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.standings.map((standing) => (
                    <tr key={standing.player_id}>
                      <td>{standing.final_rank}</td>
                      <td>
                        <Link to="/users/$userId" params={{ userId: standing.player_id }}>
                          {name(standing)}
                        </Link>
                      </td>
                      <td>
                        {standing.wins}/{standing.losses}/{standing.draws}
                      </td>
                      <td>{standing.final_status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </>
      )}
    </>
  );
}
