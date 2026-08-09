/**
 * Where a login may send somebody afterwards — A64-024.2 §8, §16.
 *
 * **An allowlist of shapes, not a filter of bad ones.** A redirect target
 * is accepted only when it is a path on this origin: it starts with a
 * single `/`, is not protocol-relative (`//evil.example`), carries no
 * scheme, and does not point back at the login form.
 *
 * Anything else falls back to the dashboard. That is what closes the open
 * redirect §16 names: a console that followed `?next=https://evil.example`
 * would be a phishing hop wearing an administrator's trust.
 *
 * `//` is the case people miss. The browser reads `//evil.example/x` as an
 * absolute URL with the current scheme, so a check for "starts with /"
 * alone lets an external host straight through.
 */

const DASHBOARD = "/";

export function safeRedirect(candidate: string | null | undefined): string {
  if (typeof candidate !== "string" || candidate.length === 0) return DASHBOARD;
  if (!candidate.startsWith("/")) return DASHBOARD;
  // Protocol-relative — an absolute URL in disguise.
  if (candidate.startsWith("//")) return DASHBOARD;
  // A backslash is normalised to a slash by some browsers, so `/\evil.com`
  // is the same trick spelled differently.
  if (candidate.startsWith("/\\")) return DASHBOARD;
  if (candidate.includes(":")) return DASHBOARD;
  // Returning to the login form after logging in is a loop.
  if (candidate === "/login" || candidate.startsWith("/login?")) return DASHBOARD;
  return candidate;
}
