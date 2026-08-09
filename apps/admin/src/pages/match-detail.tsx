import { Link, useParams } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { type AdminMatchDetail, fetchMatch } from "@/shared/api/client";
import { useTranslation } from "@/shared/i18n";
import { ErrorNotice } from "@/shared/ui/error-notice";

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

  const moment = (value: string | null) =>
    value === null ? t("matches.unknown") : new Date(value).toLocaleString(locale);

  return (
    <>
      <p>
        <Link to="/matches">{t("matches.back")}</Link>
      </p>

      {state === "loading" && <p role="status">{t("matches.loading")}</p>}
      {state === "error" && <ErrorNotice message={t("matches.error")} />}

      {state === "ready" && match !== null && (
        <>
          <h2>{t("matches.sectionMatch")}</h2>
          <dl className="facts">
            <dt>{t("matches.matchId")}</dt>
            <dd>
              <code>{match.match_id}</code>
            </dd>
            <dt>{t("matches.colStatus")}</dt>
            <dd>{match.status}</dd>
            <dt>{t("matches.origin")}</dt>
            <dd>{match.origin}</dd>
          </dl>

          <section>
            <h3>{t("matches.sectionParticipants")}</h3>
            <dl className="facts">
              {[match.light, match.dark].map((player) => (
                <div key={player.side} style={{ display: "contents" }}>
                  <dt>{t(player.side === "light" ? "matches.light" : "matches.dark")}</dt>
                  <dd>
                    <Link to="/users/$userId" params={{ userId: player.player_id }}>
                      {player.display_name ?? player.username ?? player.player_id}
                    </Link>
                  </dd>
                </div>
              ))}
            </dl>
          </section>

          <section>
            <h3>{t("matches.sectionConfig")}</h3>
            <dl className="facts">
              <dt>{t("matches.variant")}</dt>
              <dd>{match.variant}</dd>
              <dt>{t("matches.ratedLabel")}</dt>
              <dd>{t(match.rated ? "matches.rated" : "matches.casual")}</dd>
              {match.speed_class !== null && (
                <>
                  <dt>{t("matches.speed")}</dt>
                  <dd>{match.speed_class}</dd>
                </>
              )}
              {match.time_control !== null && (
                <>
                  <dt>{t("matches.timeControl")}</dt>
                  <dd>
                    {Math.round(match.time_control.initial_ms / 1000)}s +{" "}
                    {Math.round(match.time_control.increment_ms / 1000)}s
                  </dd>
                </>
              )}
            </dl>
          </section>

          <section>
            <h3>{t("matches.sectionResult")}</h3>
            {match.outcome === null && match.winner === null ? (
              <p className="muted">{t("matches.noResult")}</p>
            ) : (
              <dl className="facts">
                {match.outcome !== null && (
                  <>
                    <dt>{t("matches.outcome")}</dt>
                    <dd>{match.outcome}</dd>
                  </>
                )}
                {match.winner !== null && (
                  <>
                    <dt>{t("matches.winner")}</dt>
                    <dd>{t(match.winner === "light" ? "matches.light" : "matches.dark")}</dd>
                  </>
                )}
                {match.termination_reason !== null && (
                  <>
                    <dt>{t("matches.reason")}</dt>
                    <dd>{match.termination_reason}</dd>
                  </>
                )}
              </dl>
            )}
          </section>

          <section>
            <h3>{t("matches.sectionTimeline")}</h3>
            <dl className="facts">
              <dt>{t("matches.created")}</dt>
              <dd>{moment(match.created_at)}</dd>
              <dt>{t("matches.settled")}</dt>
              <dd>{moment(match.settled_at)}</dd>
              <dt>{t("matches.ended")}</dt>
              <dd>{moment(match.ended_at)}</dd>
              <dt>{t("matches.plies")}</dt>
              <dd>{match.ply_number}</dd>
            </dl>
          </section>
        </>
      )}
    </>
  );
}
