import { Link, useParams } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import {
  type AdminMatchDetail,
  type AdminMatchParticipant,
  fetchMatch,
} from "@/shared/api/client";
import { useTranslation } from "@/shared/i18n";
import { useVocab } from "@/features/vocabulary";
import { Icon } from "@/shared/ui/icon";
import { PageHeader } from "@/shared/ui/page-header";
import { StatusBadge, type Tone } from "@/shared/ui/status-badge";
import { ErrorState, LoadingSkeleton } from "@/shared/ui/states";

/**
 * One match — A64-024.4 §14.
 *
 * Sections rather than a JSON dump, and **only the sections the API
 * returns**: there is no Moves/Replay heading, because the detail response
 * carries no move list. `MatchReplayReader` applies every ply through the
 * engine, so folding it in would replay a game on every detail open — §10
 * asks for replay only where the architecture supports it naturally, and
 * here it would make the cheap read expensive.
 *
 * Participants link to `/users/$userId`, which is a page with its own guard
 * and its own decision about what to show. That is why this page carries no
 * email and no account state: the operator who needs them is one click
 * away, on the surface that owns them.
 *
 * Read-only. No control on this page changes anything.
 */
/** The same tones the listing uses, so a status reads identically in both. */
const STATUS_TONES: Record<string, Tone> = {
  active: "success",
  completed: "neutral",
  pending_acceptance: "info",
  declined: "danger",
  expired: "warning",
};

export function MatchDetailPage() {
  const { t, locale } = useTranslation();
  const { matchId } = useParams({ strict: false }) as { matchId: string };

  const [match, setMatch] = useState<AdminMatchDetail | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");

  useEffect(() => {
    const controller = new AbortController();
    setState("loading");
    void fetchMatch(matchId, controller.signal).then((outcome) => {
      if (controller.signal.aborted) return;
      if (outcome.status === "ok") {
        setMatch(outcome.value);
        setState("ready");
        return;
      }
      setState("error");
    });
    return () => controller.abort();
  }, [matchId]);

  const vocab = useVocab();

  /** The conventional notation an operator reads, from the real values. */
  const timeControl = (control: { initial_ms: number; increment_ms: number }) =>
    `${String(Math.round(control.initial_ms / 60000))}+${String(Math.round(control.increment_ms / 1000))}`;

  const moment = (value: string | null) =>
    value === null ? t("matches.unknown") : new Date(value).toLocaleString(locale);

  const seat = (player: AdminMatchParticipant) =>
    player.display_name ?? player.username ?? player.player_id.slice(0, 8);

  return (
    <>
      <p className="detail-links">
        <Link className="action subtle" to="/matches">
          {t("matches.back")}
        </Link>
      </p>

      {state === "loading" && <LoadingSkeleton rows={4} label={t("matches.loading")} />}
      {state === "error" && <ErrorState title={t("matches.error")} />}

      {state === "ready" && match !== null && (
        <>
          {/* The page identifies the *game*, not the row: the matchup is the
              heading and the id is metadata, which is the opposite of what
              A64-024.4 shipped. */}
          <PageHeader
            title={`${seat(match.light)} ${t("matches.versus")} ${seat(match.dark)}`}
            description={`${vocab("variant", match.variant)} · ${t(
              match.rated ? "matches.rated" : "matches.casual",
            )} · ${vocab("matchOrigin", match.origin)}`}
            actions={
              <StatusBadge
                label={t(
                  `matches.statusLabel.${match.status}` as "matches.statusLabel.completed",
                )}
                tone={STATUS_TONES[match.status] ?? "neutral"}
              />
            }
          />

          <div className="detail-grid">
            <section className="panel">
              <div className="panel__head">
                <h3>
                  <Icon name="users" size={16} />
                  {t("matches.sectionParticipants")}
                </h3>
              </div>
              <div className="panel__body">
                <ul className="seats">
                  {[match.light, match.dark].map((player) => (
                    <li key={player.player_id}>
                      <span
                        className="matchup__pip"
                        data-side={player.side}
                        aria-hidden="true"
                      />
                      <span className="cell-primary">
                        <Link to="/users/$userId" params={{ userId: player.player_id }}>
                          <strong>{seat(player)}</strong>
                        </Link>
                        <span>{vocab("side", player.side)}</span>
                      </span>
                      {match.winner === player.side && (
                        <StatusBadge label={t("matches.winner")} tone="success" />
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            </section>

            <section className="panel">
              <div className="panel__head">
                <h3>
                  <Icon name="success" size={16} />
                  {t("matches.sectionResult")}
                </h3>
              </div>
              <div className="panel__body">
                {match.outcome === null && match.winner === null ? (
                  <p className="muted">{t("matches.noResult")}</p>
                ) : (
                  <dl className="facts">
                    {match.outcome !== null && (
                      <>
                        <dt>{t("matches.outcome")}</dt>
                        <dd>{vocab("outcome", match.outcome)}</dd>
                      </>
                    )}
                    {match.winner !== null && (
                      <>
                        <dt>{t("matches.winner")}</dt>
                        <dd>{vocab("side", match.winner)}</dd>
                      </>
                    )}
                    {match.termination_reason !== null && (
                      <>
                        <dt>{t("matches.termination")}</dt>
                        <dd>{vocab("termination", match.termination_reason)}</dd>
                      </>
                    )}
                  </dl>
                )}
              </div>
            </section>

            <section className="panel">
              <div className="panel__head">
                <h3>
                  <Icon name="settings" size={16} />
                  {t("matches.sectionConfig")}
                </h3>
              </div>
              <div className="panel__body">
                <dl className="facts">
                  <dt>{t("matches.variant")}</dt>
                  <dd>{vocab("variant", match.variant)}</dd>
                  <dt>{t("matches.colMode")}</dt>
                  <dd>{t(match.rated ? "matches.rated" : "matches.casual")}</dd>
                  {match.speed_class !== null && (
                    <>
                      <dt>{t("matches.speed")}</dt>
                      <dd>{vocab("speedClass", match.speed_class)}</dd>
                    </>
                  )}
                  {match.time_control !== null && (
                    <>
                      <dt>{t("matches.timeControl")}</dt>
                      <dd>{timeControl(match.time_control)}</dd>
                    </>
                  )}
                  <dt>{t("matches.matchId")}</dt>
                  <dd className="ref">{match.match_id}</dd>
                </dl>
              </div>
            </section>

            <section className="panel">
              <div className="panel__head">
                <h3>
                  <Icon name="audit" size={16} />
                  {t("matches.sectionTimeline")}
                </h3>
              </div>
              <div className="panel__body">
                <dl className="facts">
                  <dt>{t("matches.created")}</dt>
                  <dd>{moment(match.created_at)}</dd>
                  <dt>{t("matches.accepted")}</dt>
                  <dd>{moment(match.settled_at)}</dd>
                  <dt>{t("matches.ended")}</dt>
                  <dd>{moment(match.ended_at)}</dd>
                  <dt>{t("matches.plies")}</dt>
                  <dd>{match.ply_number}</dd>
                </dl>
              </div>
            </section>
          </div>
        </>
      )}
    </>
  );
}
