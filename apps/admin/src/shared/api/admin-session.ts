/**
 * The one call this app makes before rendering anything — A64-024.1 §6.
 *
 * `GET /api/v1/admin/me` is **server-authoritative**. It is not a hint the
 * client may override, and there is no local flag, no stored role and no
 * token claim this app inspects instead: the backend guard decides, and
 * this reads its answer.
 *
 * That matters more than it sounds. Hiding a route is not authorization —
 * every admin API independently enforces `CurrentAdmin`, so a player who
 * reached this bundle, edited its state and rendered the shell would still
 * be refused by every request the shell makes. What this call buys is that
 * they never see it at all.
 */

/** Exactly the fields `AdminSessionResponse` publishes — no more. */
export interface AdminSession {
  id: string;
  username: string;
  display_name: string | null;
  roles: string[];
}

export type SessionOutcome =
  | { status: "authorized"; session: AdminSession }
  /** Authenticated, and not an administrator — a `403`. */
  | { status: "forbidden" }
  /** No credential, or one that no longer verifies — a `401`. */
  | { status: "unauthenticated" }
  /** The server could not be reached, or answered something unexpected. */
  | { status: "unavailable" };

/**
 * Asks the server who this session administers as.
 *
 * **Never throws.** Every outcome a caller must branch on is a value,
 * because a shell that has to `try/catch` around its own authorization
 * check is a shell whose failure path renders privileged chrome.
 *
 * `credentials: "include"` so the session cookie travels. The cookie is
 * host-only and this app has its own origin, so what travels is *this*
 * app's session and never `apps/web`'s.
 */
export async function fetchAdminSession(signal?: AbortSignal): Promise<SessionOutcome> {
  let response: Response;
  try {
    response = await fetch("/api/v1/admin/me", {
      credentials: "include",
      headers: { Accept: "application/json" },
      // Privileged answers are never reused from a cache — the server
      // sends `no-store` and this asks for the same on the way out.
      cache: "no-store",
      ...(signal ? { signal } : {}),
    });
  } catch {
    return { status: "unavailable" };
  }

  if (response.status === 401) return { status: "unauthenticated" };
  if (response.status === 403) return { status: "forbidden" };
  if (!response.ok) return { status: "unavailable" };

  try {
    const body = (await response.json()) as { data?: AdminSession } | AdminSession;
    // The platform wraps successful responses in `{ data, meta }`; the
    // unwrapped form is accepted too so a change to the envelope is a
    // rendering difference rather than a lockout.
    const session = "data" in body && body.data ? body.data : (body as AdminSession);
    if (typeof session?.id !== "string" || !Array.isArray(session.roles)) {
      return { status: "unavailable" };
    }
    return { status: "authorized", session };
  } catch {
    return { status: "unavailable" };
  }
}
