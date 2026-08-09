/**
 * The admin console's API client — A64-024.2 §2, §3.
 *
 * ## The contract, as the backend actually publishes it
 *
 * A64-024.1's client called `/admin/me` with `credentials: "include"` and
 * nothing else, which could never have worked: the refresh cookie is
 * scoped to `path=/api/v1/auth/browser`, so it is **not sent** to
 * `/api/v1/admin/me`, and `CurrentUser` authenticates from an
 * `Authorization` header. That is fixed here.
 *
 *     POST /auth/browser/login    credentials -> host-only refresh cookie
 *                                 + an access token in the body
 *     POST /auth/browser/refresh  the cookie -> a fresh access token
 *     GET  /admin/me              Authorization: Bearer <access token>
 *     POST /auth/browser/logout   revokes the session and clears the cookie
 *
 * ## The access token never leaves memory
 *
 * Held by `session-store`, in a closure. Not `localStorage`, not
 * `sessionStorage`, not a cookie this app writes — the same rule
 * `apps/web` keeps, and for the same reason: anything a script can read is
 * something an injected script can exfiltrate. The refresh half stays in
 * an `HttpOnly` cookie this app cannot read at all.
 */

import { accessToken } from "@/app/session-store";

const API = "/api/v1";

/** Every outcome a caller branches on, as a value rather than an exception. */
export type Outcome<T> =
  | { status: "ok"; value: T }
  | { status: "unauthenticated" }
  | { status: "forbidden" }
  | { status: "invalid_credentials" }
  | { status: "rate_limited" }
  /**
   * The server's safety rules said no — A64-024.6.
   *
   * Distinct from `unavailable` because they mean opposite things to an
   * operator: one is "the platform decided", the other is "we could not
   * ask". Folding them together would show a network error for a refusal
   * the console should explain.
   */
  | { status: "refused" }
  | { status: "unavailable" };

export interface AdminSession {
  id: string;
  username: string;
  display_name: string | null;
  roles: string[];
}

interface LoginBody {
  access_token: string;
}

/** The platform wraps success in `{ data, meta }`. */
function unwrap<T>(body: unknown): T | null {
  if (typeof body !== "object" || body === null) return null;
  const envelope = body as { data?: unknown };
  return (envelope.data ?? body) as T;
}

async function send(path: string, init: RequestInit): Promise<Response | null> {
  try {
    return await fetch(`${API}${path}`, {
      // The refresh cookie must travel on the auth calls. Harmless on the
      // others, where its path means it is not sent anyway.
      credentials: "include",
      // Privileged answers are never reused from a cache — the server
      // sends `no-store` and this asks for the same on the way out.
      cache: "no-store",
      ...init,
      headers: { Accept: "application/json", ...(init.headers ?? {}) },
    });
  } catch {
    return null;
  }
}

/**
 * Exchanges credentials for a session.
 *
 * `401` is reported as `invalid_credentials` rather than
 * `unauthenticated`, because the two mean different things to this app: one
 * is a form that should stay on screen with an error, the other is a
 * session that has ended. The backend answers the same `401` whether the
 * address is unknown or the password is wrong, in the same elapsed time —
 * this client does not try to tell them apart either.
 */
export async function signIn(email: string, password: string): Promise<Outcome<string>> {
  const response = await send("/auth/browser/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (response === null) return { status: "unavailable" };
  if (response.status === 401) return { status: "invalid_credentials" };
  if (response.status === 429) return { status: "rate_limited" };
  // `403` here is an account that may not sign in — disabled, or an origin
  // the backend does not trust. Neither is a credential problem.
  if (response.status === 403) return { status: "forbidden" };
  if (!response.ok) return { status: "unavailable" };

  const body = unwrap<LoginBody>(await response.json().catch(() => null));
  if (typeof body?.access_token !== "string") return { status: "unavailable" };
  return { status: "ok", value: body.access_token };
}

/**
 * Trades the refresh cookie for a fresh access token.
 *
 * **The whole of "refresh works on a protected route".** A reload loses the
 * in-memory token and keeps the cookie, so this is what turns a direct
 * navigation to `/users` into a signed-in session rather than a login form.
 */
export async function refresh(): Promise<Outcome<string>> {
  const response = await send("/auth/browser/refresh", { method: "POST" });
  if (response === null) return { status: "unavailable" };
  if (response.status === 401) return { status: "unauthenticated" };
  if (response.status === 403) return { status: "forbidden" };
  if (!response.ok) return { status: "unavailable" };

  const body = unwrap<LoginBody>(await response.json().catch(() => null));
  if (typeof body?.access_token !== "string") return { status: "unavailable" };
  return { status: "ok", value: body.access_token };
}

/** Revokes the session server-side. Never throws — see `signOut` in the shell. */
export async function signOut(): Promise<void> {
  await send("/auth/browser/logout", { method: "POST" });
}

/**
 * Who this session administers as — the **server-authoritative** answer.
 *
 * Called on every entry to a protected route and never cached, which is
 * what keeps A64-024.1's zero-staleness property: a revoked administrator
 * is refused here on their next navigation, because the guard behind this
 * reads `admin.role_assignment` rather than a token claim.
 */
export async function fetchAdminSession(): Promise<Outcome<AdminSession>> {
  const token = accessToken.get();
  if (token === null) return { status: "unauthenticated" };

  const response = await send("/admin/me", {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (response === null) return { status: "unavailable" };
  if (response.status === 401) return { status: "unauthenticated" };
  if (response.status === 403) return { status: "forbidden" };
  if (!response.ok) return { status: "unavailable" };

  const session = unwrap<AdminSession>(await response.json().catch(() => null));
  if (typeof session?.id !== "string" || !Array.isArray(session.roles)) {
    return { status: "unavailable" };
  }
  return { status: "ok", value: session };
}

// --- admin users — A64-024.3 -------------------------------------------------

export interface AdminUserSummary {
  id: string;
  username: string;
  display_name: string | null;
  email: string;
  is_active: boolean;
  is_verified: boolean;
  created_at: string;
  is_admin: boolean;
}

export interface AdminUserDetail extends AdminUserSummary {
  admin_role_granted_at: string | null;
  /** A64-024.6 — the account's **effective** standing, not its history. */
  moderation: AccountModerationState;
}

export interface AdminUserPage {
  items: AdminUserSummary[];
  next_cursor: string | null;
}

export interface UserQuery {
  q?: string;
  is_active?: boolean;
  is_verified?: boolean;
  cursor?: string;
}

/**
 * One page of accounts.
 *
 * Bearer-authenticated like every admin read, so a revoked administrator is
 * refused here on their next request — the server re-reads the role rather
 * than trusting a token claim.
 *
 * `signal` is threaded through so a superseded search can be abandoned:
 * without it a slow first request can resolve after a faster second and
 * repaint stale rows over fresh ones.
 */
export async function fetchUsers(
  query: UserQuery,
  signal?: AbortSignal,
): Promise<Outcome<AdminUserPage>> {
  const params = new URLSearchParams();
  if (query.q) params.set("q", query.q);
  if (query.is_active !== undefined) params.set("is_active", String(query.is_active));
  if (query.is_verified !== undefined) params.set("is_verified", String(query.is_verified));
  if (query.cursor) params.set("cursor", query.cursor);

  return authorizedRead<AdminUserPage>(
    `/admin/users${params.size > 0 ? `?${params.toString()}` : ""}`,
    signal,
  );
}

export async function fetchUser(
  userId: string,
  signal?: AbortSignal,
): Promise<Outcome<AdminUserDetail>> {
  return authorizedRead<AdminUserDetail>(`/admin/users/${encodeURIComponent(userId)}`, signal);
}

/** One authenticated admin read, with every outcome as a value. */
async function authorizedRead<T>(path: string, signal?: AbortSignal): Promise<Outcome<T>> {
  const token = accessToken.get();
  if (token === null) return { status: "unauthenticated" };

  const response = await send(path, {
    headers: { Authorization: `Bearer ${token}` },
    ...(signal ? { signal } : {}),
  });
  if (response === null) return { status: "unavailable" };
  if (response.status === 401) return { status: "unauthenticated" };
  if (response.status === 403) return { status: "forbidden" };
  if (!response.ok) return { status: "unavailable" };

  const value = unwrap<T>(await response.json().catch(() => null));
  return value === null ? { status: "unavailable" } : { status: "ok", value };
}

// --- admin matches — A64-024.4 -----------------------------------------------

export interface AdminMatchParticipant {
  player_id: string;
  username: string | null;
  display_name: string | null;
  side: string;
}

export interface AdminMatchSummary {
  match_id: string;
  status: string;
  variant: string;
  rated: boolean;
  origin: string;
  light: AdminMatchParticipant;
  dark: AdminMatchParticipant;
  outcome: string | null;
  winner: string | null;
  termination_reason: string | null;
  speed_class: string | null;
  ply_number: number;
  created_at: string;
  ended_at: string | null;
}

export interface AdminMatchDetail extends AdminMatchSummary {
  settled_at: string | null;
  time_control: { initial_ms: number; increment_ms: number } | null;
}

export interface AdminMatchPage {
  items: AdminMatchSummary[];
  next_cursor: string | null;
}

export interface MatchQuery {
  status?: string;
  rated?: boolean;
  origin?: string;
  participant_id?: string;
  cursor?: string;
}

export async function fetchMatches(
  query: MatchQuery,
  signal?: AbortSignal,
): Promise<Outcome<AdminMatchPage>> {
  const params = new URLSearchParams();
  if (query.status) params.set("status", query.status);
  if (query.rated !== undefined) params.set("rated", String(query.rated));
  if (query.origin) params.set("origin", query.origin);
  if (query.participant_id) params.set("participant_id", query.participant_id);
  if (query.cursor) params.set("cursor", query.cursor);

  return authorizedRead<AdminMatchPage>(
    `/admin/matches${params.size > 0 ? `?${params.toString()}` : ""}`,
    signal,
  );
}

export async function fetchMatch(
  matchId: string,
  signal?: AbortSignal,
): Promise<Outcome<AdminMatchDetail>> {
  return authorizedRead<AdminMatchDetail>(
    `/admin/matches/${encodeURIComponent(matchId)}`,
    signal,
  );
}

// --- admin tournaments — A64-024.5 -------------------------------------------

export interface AdminTournamentSummary {
  tournament_id: string;
  name: string;
  format: string;
  variant: string;
  speed_class: string;
  status: string;
  rated: boolean;
  capacity: number;
  entrant_count: number;
  registration_deadline: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

export interface AdminEntrantView {
  player_id: string;
  username: string | null;
  display_name: string | null;
  status: string;
  seed_number: number | null;
  registered_at: string;
  withdrawn_at: string | null;
}

export interface AdminRoundView {
  round_number: number;
  status: string;
  pairing_count: number;
  published_at: string | null;
  started_at: string | null;
  completed_at: string | null;
}

/**
 * One bracket node.
 *
 * `round_number` and `slot` are its identity, and the tree follows from
 * them: the parent is `(round_number + 1, slot >> 1)`. The backend
 * publishes the coordinates rather than the edges, so a view derives the
 * structure from the same arithmetic the domain uses instead of trusting a
 * second description of it.
 */
export interface AdminPairingView {
  round_number: number;
  slot: number;
  light_player_id: string | null;
  dark_player_id: string | null;
  light_seed: number | null;
  dark_seed: number | null;
  winner_id: string | null;
  advancement_reason: string | null;
  match_ids: string[];
}

export interface AdminStandingView {
  player_id: string;
  username: string | null;
  display_name: string | null;
  final_rank: number;
  seed_number: number;
  elimination_round: number | null;
  eliminated_by_player_id: string | null;
  wins: number;
  losses: number;
  draws: number;
  final_status: string;
}

export interface AdminTournamentDetail {
  tournament: AdminTournamentSummary;
  entrants: AdminEntrantView[];
  rounds: AdminRoundView[];
  pairings: AdminPairingView[];
  standings: AdminStandingView[];
}

export interface AdminTournamentPage {
  items: AdminTournamentSummary[];
  next_cursor: string | null;
}

export interface TournamentQuery {
  status?: string;
  format?: string;
  variant?: string;
  rated?: boolean;
  cursor?: string;
}

export async function fetchTournaments(
  query: TournamentQuery,
  signal?: AbortSignal,
): Promise<Outcome<AdminTournamentPage>> {
  const params = new URLSearchParams();
  if (query.status) params.set("status", query.status);
  if (query.format) params.set("format", query.format);
  if (query.variant) params.set("variant", query.variant);
  if (query.rated !== undefined) params.set("rated", String(query.rated));
  if (query.cursor) params.set("cursor", query.cursor);

  return authorizedRead<AdminTournamentPage>(
    `/admin/tournaments${params.size > 0 ? `?${params.toString()}` : ""}`,
    signal,
  );
}

export async function fetchTournament(
  tournamentId: string,
  signal?: AbortSignal,
): Promise<Outcome<AdminTournamentDetail>> {
  return authorizedRead<AdminTournamentDetail>(
    `/admin/tournaments/${encodeURIComponent(tournamentId)}`,
    signal,
  );
}

// --- admin audit — A64-024.8 -------------------------------------------------

export interface AdminAuditActor {
  type: string;
  account_id: string | null;
  username: string | null;
}

export interface AdminAuditSubject {
  type: string;
  ref: string;
  username: string | null;
}

export interface AdminAuditEntry {
  id: string;
  action: string;
  outcome: string;
  actor: AdminAuditActor;
  subject: AdminAuditSubject;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  correlation_id: string | null;
  created_at: string;
}

export interface AdminAuditPage {
  items: AdminAuditEntry[];
  next_cursor: string | null;
}

export interface AuditQuery {
  action?: string;
  actor_id?: string;
  subject_type?: string;
  subject_ref?: string;
  cursor?: string;
}

/**
 * One page of the audit trail.
 *
 * `subject_ref` travels only with `subject_type` — the server refuses the
 * pair apart, because a filter that quietly did nothing would show an
 * operator the whole trail while they believed they were reading one
 * account's history.
 */
export async function fetchAuditEntries(
  query: AuditQuery,
  signal?: AbortSignal,
): Promise<Outcome<AdminAuditPage>> {
  const params = new URLSearchParams();
  if (query.action) params.set("action", query.action);
  if (query.actor_id) params.set("actor_id", query.actor_id);
  if (query.subject_type && query.subject_ref) {
    params.set("subject_type", query.subject_type);
    params.set("subject_ref", query.subject_ref);
  }
  if (query.cursor) params.set("cursor", query.cursor);

  return authorizedRead<AdminAuditPage>(
    `/admin/audit${params.size > 0 ? `?${params.toString()}` : ""}`,
    signal,
  );
}

// --- admin moderation — A64-024.6 --------------------------------------------

export interface AdminModerationCase {
  id: string;
  category: string;
  decision: string;
  reasoning: string;
  opened_by: string;
  opened_by_username: string | null;
  opened_at: string;
}

export interface AdminSanction {
  id: string;
  player_id: string;
  username: string | null;
  kind: string;
  is_effective: boolean;
  starts_at: string;
  expires_at: string | null;
  lifted_at: string | null;
  lifted_by: string | null;
  case: AdminModerationCase;
}

export interface AccountModerationState {
  is_restricted: boolean;
  restriction: AdminSanction | null;
}

export interface AdminSanctionPage {
  items: AdminSanction[];
  next_cursor: string | null;
}

/** The bounded reason vocabulary the server accepts. Localised here. */
export const MODERATION_CATEGORIES = [
  "cheating",
  "abuse",
  "account_compromise",
  "policy_violation",
  "other",
] as const;

export type ModerationCategory = (typeof MODERATION_CATEGORIES)[number];

export interface RestrictionQuery {
  effective_only?: boolean;
  cursor?: string;
}

export async function fetchRestrictions(
  query: RestrictionQuery,
  signal?: AbortSignal,
): Promise<Outcome<AdminSanctionPage>> {
  const params = new URLSearchParams();
  if (query.effective_only === false) params.set("effective_only", "false");
  if (query.cursor) params.set("cursor", query.cursor);

  return authorizedRead<AdminSanctionPage>(
    `/admin/moderation${params.size > 0 ? `?${params.toString()}` : ""}`,
    signal,
  );
}

export interface RestrictAccountInput {
  category: ModerationCategory;
  reasoning: string;
  /** Omitted for an indefinite restriction. The server computes the expiry. */
  duration_hours?: number;
}

/**
 * Withholds access from an account.
 *
 * **No actor field.** The server takes it from the admin session, so there
 * is nothing this client could send that changes who is recorded as having
 * decided — and nothing it could get wrong.
 */
export async function restrictAccount(
  userId: string,
  input: RestrictAccountInput,
): Promise<Outcome<AdminSanction>> {
  return authorizedWrite<AdminSanction>(
    `/admin/users/${encodeURIComponent(userId)}/restrict`,
    input,
  );
}

/** Lifts the live restriction. No body: there is nothing to decide. */
export async function restoreAccount(userId: string): Promise<Outcome<AdminSanction>> {
  return authorizedWrite<AdminSanction>(
    `/admin/users/${encodeURIComponent(userId)}/restore`,
    {},
  );
}

/**
 * One authenticated admin write, with every outcome as a value.
 *
 * `409` and `422` are reported as `refused` rather than folded into
 * `unavailable`: they are the platform's safety rules answering — already
 * restricted, the last administrator, a restriction of oneself — and the
 * console must show the operator that the system decided, not that the
 * network failed.
 */
async function authorizedWrite<T>(path: string, body: unknown): Promise<Outcome<T>> {
  const token = accessToken.get();
  if (token === null) return { status: "unauthenticated" };

  const response = await send(path, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (response === null) return { status: "unavailable" };
  if (response.status === 401) return { status: "unauthenticated" };
  if (response.status === 403) return { status: "forbidden" };
  if (response.status === 409 || response.status === 422) return { status: "refused" };
  if (!response.ok) return { status: "unavailable" };

  const value = unwrap<T>(await response.json().catch(() => null));
  return value === null ? { status: "unavailable" } : { status: "ok", value };
}

// --- admin notification operations — A64-024.7 -------------------------------

export interface AdminPushDeliveryView {
  subscription_id: string;
  status: string;
  outcome: string | null;
  attempt_count: number;
  next_attempt_at: string | null;
  last_attempt_at: string | null;
  /** When a push service **accepted** the request — never a device acknowledgement. */
  accepted_at: string | null;
  created_at: string;
  can_retry: boolean;
  device_first_seen_at: string | null;
  device_last_seen_at: string | null;
  device_revoked_at: string | null;
}

export interface AdminNotificationSummary {
  id: string;
  recipient_id: string;
  recipient_username: string | null;
  type: string;
  category: string;
  created_at: string;
  read_at: string | null;
  push_capable: boolean;
  push_summary: string;
  delivery_count: number;
}

export interface AdminNotificationDetail {
  id: string;
  recipient_id: string;
  recipient_username: string | null;
  type: string;
  category: string;
  target_type: string;
  target_ref: string | null;
  source_event_id: string;
  created_at: string;
  read_at: string | null;
  push_capable: boolean;
  deliveries: AdminPushDeliveryView[];
}

export interface AdminNotificationPage {
  items: AdminNotificationSummary[];
  next_cursor: string | null;
}

export interface NotificationQuery {
  recipient_id?: string;
  failed_push_only?: boolean;
  cursor?: string;
}

export async function fetchNotifications(
  query: NotificationQuery,
  signal?: AbortSignal,
): Promise<Outcome<AdminNotificationPage>> {
  const params = new URLSearchParams();
  if (query.recipient_id) params.set("recipient_id", query.recipient_id);
  if (query.failed_push_only) params.set("failed_push_only", "true");
  if (query.cursor) params.set("cursor", query.cursor);

  return authorizedRead<AdminNotificationPage>(
    `/admin/notifications${params.size > 0 ? `?${params.toString()}` : ""}`,
    signal,
  );
}

export async function fetchNotification(
  notificationId: string,
  signal?: AbortSignal,
): Promise<Outcome<AdminNotificationDetail>> {
  return authorizedRead<AdminNotificationDetail>(
    `/admin/notifications/${encodeURIComponent(notificationId)}`,
    signal,
  );
}

/**
 * Queues one more attempt at an already-recorded delivery.
 *
 * **Not a send.** There is no body, and there is nothing to put in one: the
 * recipient, the type, the payload and the destination are already stored
 * and this changes none of them.
 */
export async function retryNotificationDelivery(
  notificationId: string,
  subscriptionId: string,
): Promise<Outcome<AdminPushDeliveryView>> {
  return authorizedWrite<AdminPushDeliveryView>(
    `/admin/notifications/${encodeURIComponent(notificationId)}/deliveries/` +
      `${encodeURIComponent(subscriptionId)}/retry`,
    {},
  );
}

// --- admin dashboard — A64-024.9 ---------------------------------------------

export interface AdminDashboardActivity {
  id: string;
  action: string;
  outcome: string;
  actor_type: string;
  actor_id: string | null;
  actor_username: string | null;
  subject_type: string;
  subject_ref: string;
  created_at: string;
}

export interface AdminDashboard {
  accounts: { registered_last_day: number; registered_last_week: number };
  matches: { active: number; awaiting_acceptance: number };
  tournaments: { registration_open: number; in_progress: number };
  attention: { restrictions_in_force: number; push_deliveries_retry_exhausted: number };
  recent_activity: AdminDashboardActivity[];
  generated_at: string;
}

/**
 * The operator overview — **one** request.
 *
 * Six counts and the ten most recent privileged actions, composed on the
 * server. Six separate list calls would be six round trips, and each would
 * return a page rather than a count.
 */
export async function fetchDashboard(signal?: AbortSignal): Promise<Outcome<AdminDashboard>> {
  return authorizedRead<AdminDashboard>("/admin/dashboard", signal);
}
