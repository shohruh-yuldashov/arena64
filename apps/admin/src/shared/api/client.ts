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
