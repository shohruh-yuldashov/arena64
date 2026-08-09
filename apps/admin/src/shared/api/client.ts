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
