/**
 * Where it is safe to send somebody after they sign in — A64-020.2 §10.
 *
 * ## The vulnerability this closes
 *
 * A guard redirects an anonymous visitor to `/login?next=/tournaments`, and
 * the login page sends them to `next` afterwards. If `next` is taken at
 * face value, `https://arena64.uz/login?next=https://evil.example/login`
 * is a link that shows the real site's real login form and then hands the
 * user to a copy of it — an **open redirect**, and one of the most
 * effective phishing primitives there is, because every visible signal up
 * to the final hop is genuine.
 *
 * So `next` is validated, not sanitised. Anything that is not an in-app
 * path is discarded and the caller falls back to the home route; there is
 * no attempt to "fix" a suspicious value, because a rewriting rule is
 * something an attacker can compose against.
 *
 * ## What is rejected, and why each one
 *
 *     https://evil.example    absolute — another origin entirely
 *     //evil.example          protocol-relative; the browser reads this as
 *                             `https://evil.example`, and it is the case a
 *                             naive `startsWith("/")` check lets through
 *     javascript:alert(1)     a scheme, not a path
 *     \\evil.example          backslashes, which some browsers normalise
 *                             to forward slashes before resolving
 *     /login                  in-app but circular — a redirect back to the
 *                             page that redirected here
 *     %2f%2fevil.example      double-encoded; decoded once and re-checked
 */
const AUTH_PATHS = [
  "/login",
  "/register",
  "/verify-email",
  "/forgot-password",
  "/reset-password",
];

export const DEFAULT_REDIRECT = "/";

function hasWhitespaceOrControl(value: string): boolean {
  for (const character of value) {
    const code = character.codePointAt(0) ?? 0;
    if (code <= 0x20 || code === 0x7f) return true;
  }
  return /\s/.test(value);
}

export function safeRedirect(next: string | null | undefined): string {
  if (next === null || next === undefined || next === "") {
    return DEFAULT_REDIRECT;
  }

  let candidate = next;
  try {
    // One decode pass, so `%2F%2Fevil.example` is judged as `//evil.example`
    // rather than as an opaque string that happens to start with `%`.
    candidate = decodeURIComponent(next);
  } catch {
    // Malformed percent-encoding. Not something to repair — a value this
    // app cannot read is a value it will not navigate to.
    return DEFAULT_REDIRECT;
  }

  // Control characters and whitespace are stripped by some browsers before
  // the URL is resolved, so `/\tjavascript:...` can become a scheme once the
  // tab is removed. Refused rather than trimmed — trimming is a rewriting
  // rule, and a rewriting rule is something an attacker composes against.
  if (hasWhitespaceOrControl(candidate)) {
    return DEFAULT_REDIRECT;
  }

  // Must be an absolute in-app path. Not a relative one: `foo/bar` resolves
  // against whatever the current page happens to be, which is a different
  // destination depending on where the user was.
  if (!candidate.startsWith("/")) {
    return DEFAULT_REDIRECT;
  }

  // `//host` and `/\host` are network-path references — the browser reads
  // both as another origin, and both begin with `/`.
  if (candidate.startsWith("//") || candidate.startsWith("/\\")) {
    return DEFAULT_REDIRECT;
  }

  // A backslash anywhere in the authority position is normalised to `/` by
  // some browsers, so it is refused outright rather than reasoned about.
  if (candidate.includes("\\")) {
    return DEFAULT_REDIRECT;
  }

  // Sending somebody from `/login` back to `/login` is a loop; from
  // `/login` to `/register` is a confusing bounce. Neither is a
  // destination anyone asked for.
  if (AUTH_PATHS.some((path) => candidate === path || candidate.startsWith(`${path}?`))) {
    return DEFAULT_REDIRECT;
  }

  return candidate;
}
