import { api } from "@/shared/api";
import type { components } from "@/shared/api/generated/schema";

/**
 * The tournament endpoints — A64-020.6 §1.
 *
 * Six reads and two writes, every one of them a real route on
 * `app/modules/tournament/presentation/router.py`. Types come from the
 * generated schema and never from a hand-written interface: a field the
 * backend renames must break this file at compile time rather than at a
 * user's screen.
 *
 * ## What is not here
 *
 * Creating a tournament, opening or closing registration, seeding and
 * starting. Those are not "not implemented yet" — they are deliberately
 * **not HTTP at all**: this platform has no administrator role, so an
 * endpoint behind `CurrentUser` would let every registered player close
 * somebody else's registration. They live in `app/operator/tournament.py`,
 * reachable by whoever can run a process on the host. §2 defers the
 * player-facing half to A64-023 Administration, and there is nothing here
 * to call in the meantime.
 */
type Schemas = components["schemas"];

export type Tournament = Schemas["TournamentResponse"];
export type TournamentPage = Schemas["TournamentListResponse"];
export type TournamentStatus = Schemas["TournamentStatus"];
export type TournamentFormat = Schemas["TournamentFormat"];

export type Bracket = Schemas["BracketResponse"];
export type BracketRound = Schemas["RoundResponse"];
export type BracketNode = Schemas["BracketNodeResponse"];
export type BracketAttempt = Schemas["AttemptResponse"];
export type TournamentParticipant = Schemas["TournamentParticipantResponse"];

export type Standings = Schemas["StandingsResponse"];
export type Standing = Schemas["StandingResponse"];

export type Registration = Schemas["RegistrationResponse"];

/**
 * What the lobby may be narrowed by — §6.
 *
 * A **closed** set of five, mirroring the backend's `TournamentFilter`
 * exactly. Each is an enum or a boolean the tournament already stores, so
 * every combination is a predicate over indexed columns and an unknown
 * value is a `422` rather than an empty page.
 *
 * There is no free-text search and no caller-chosen ordering, because the
 * backend offers neither — §6 forbids expanding the API for a decorative
 * control, and a client-side sort over one page would order twenty rows and
 * call it the ranking of two thousand.
 */
export interface TournamentFilters {
  status?: TournamentStatus;
  format?: TournamentFormat;
  variant?: string;
  speed_class?: string;
  rated?: boolean;
}

export function readTournaments(
  filters: TournamentFilters = {},
  options: { after?: string | null; limit?: number } = {},
): Promise<TournamentPage> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null) query.set(key, String(value));
  }
  // **Sent back verbatim** — the cursor is base64 of two server-side
  // columns and this client never looks inside it (§4).
  if (options.after) query.set("after", options.after);
  if (options.limit) query.set("limit", String(options.limit));

  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  return api.get<TournamentPage>(`/tournaments${suffix}`);
}

export function readTournament(tournamentId: string): Promise<Tournament> {
  return api.get<Tournament>(`/tournaments/${tournamentId}`);
}

export function readBracket(tournamentId: string): Promise<Bracket> {
  return api.get<Bracket>(`/tournaments/${tournamentId}/bracket`);
}

export function readStandings(tournamentId: string): Promise<Standings> {
  return api.get<Standings>(`/tournaments/${tournamentId}/standings`);
}

/**
 * The viewer's own entry, or `null` when they never entered.
 *
 * `404` is translated to `null` **here and only here**, because it is the
 * one place that knows the question was "am I in this?" — a normal question
 * whose negative answer is not a failure. Everything above receives
 * `Registration | null` and branches on a value rather than on a thrown
 * error, which is what keeps "not registered" out of the error path where
 * it would render as an outage.
 *
 * Every other failure — `401`, a network fault, a `500` — still throws.
 */
export async function readMyRegistration(tournamentId: string): Promise<Registration | null> {
  try {
    return await api.get<Registration>(`/tournaments/${tournamentId}/registrations/me`);
  } catch (error) {
    if (isNotFound(error)) return null;
    throw error;
  }
}

/**
 * Enter, as **the authenticated player**.
 *
 * No body and no player id: the server reads the entrant from the access
 * token, so there is nothing here that could name somebody else.
 */
export function enterTournament(tournamentId: string): Promise<Registration> {
  return api.post<Registration>(`/tournaments/${tournamentId}/registrations`);
}

export function withdrawFromTournament(tournamentId: string): Promise<Registration> {
  return api.delete<Registration>(`/tournaments/${tournamentId}/registrations/me`);
}

function isNotFound(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    (error as { status: number | null }).status === 404
  );
}
