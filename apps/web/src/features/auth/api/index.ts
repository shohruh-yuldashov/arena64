import type { BrowserSession } from "@/entities/session";
import { api } from "@/shared/api";
import type { components } from "@/shared/api/generated/schema";

/**
 * Every call this feature makes, in one file.
 *
 * Thin on purpose: each function is a URL, a payload type from the
 * **generated** schema, and nothing else. No error handling — `request`
 * already normalises — and no state, which is `model/`'s.
 *
 * `withCredentials` is already on the shared client, so the refresh cookie
 * travels with these automatically. It is set globally rather than here
 * because a per-call flag is a per-call thing to forget, and the cookie is
 * scoped by `Path` on the server side anyway.
 */
type Schemas = components["schemas"];
type UserRead = Schemas["UserRead"];

const BROWSER = "/auth/browser";

export function login(payload: Schemas["LoginRequest"]): Promise<BrowserSession> {
  return api.post<BrowserSession>(`${BROWSER}/login`, payload);
}

export function register(payload: Schemas["RegisterRequest"]): Promise<BrowserSession> {
  return api.post<BrowserSession>(`${BROWSER}/register`, payload);
}

/**
 * Exchanges the `HttpOnly` cookie for a new access token.
 *
 * **No argument**, and that is the contract: the credential is the cookie,
 * and a parameter here would be a way for the page to supply one — which
 * would mean the page could hold one.
 */
export function refresh(): Promise<BrowserSession> {
  return api.post<BrowserSession>(`${BROWSER}/refresh`);
}

export function logout(): Promise<void> {
  return api.post<void>(`${BROWSER}/logout`);
}

export function logoutEverywhere(): Promise<void> {
  return api.post<void>(`${BROWSER}/logout-all`);
}

export function verifyEmail(payload: Schemas["VerifyEmailRequest"]): Promise<unknown> {
  return api.post(`/auth/email/verify`, payload);
}

/**
 * Submits the six digits from the verification email — A64-021.5H.
 *
 * **Authenticated and address-free.** The session says whose challenge this
 * is, so there is no field in which to name somebody else's account and no
 * way to discover whether an address has one open.
 */
export function verifyEmailCode(payload: Schemas["VerifyCodeRequest"]): Promise<UserRead> {
  return api.post<UserRead>("/auth/email/verify-code", payload);
}

/** Asks for a fresh code. `409` inside the sixty-second cooldown. */
export function resendVerificationCode(): Promise<unknown> {
  return api.post("/auth/email/resend-code");
}

export function resendVerification(
  payload: Schemas["ResendVerificationRequest"],
): Promise<unknown> {
  return api.post(`/auth/email/resend`, payload);
}

export function forgotPassword(payload: Schemas["ForgotPasswordRequest"]): Promise<unknown> {
  return api.post(`/auth/password/forgot`, payload);
}

export function resetPassword(payload: Schemas["ResetPasswordRequest"]): Promise<unknown> {
  return api.post(`/auth/password/reset`, payload);
}

/** The path the interceptor must never refresh against — see `model/`. */
export const REFRESH_PATH = `${BROWSER}/refresh`;
