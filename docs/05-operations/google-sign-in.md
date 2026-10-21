# Google Sign-In — Setup and Verification

> **Status:** current. Opened by A64-031.E1.
> **Owner:** platform engineering.
> **Decision:** [ADR-008](../07-decisions/ADR-008-google-sign-in.md). **Contract:**
> [`specs/authentication.md`](../../specs/authentication.md) §Google sign-in.

This covers what an operator configures, in the Google Cloud console and in each tier,
and how to verify the flow against real Google. CI never contacts Google: it tests every
check against a local RSA key and a mocked provider. A tier has therefore **not** been
verified until the manual check in §4 has passed on it.

---

## 1. What the platform needs

| Variable | Secret | Notes |
| --- | --- | --- |
| `GOOGLE_OAUTH_CLIENT_ID` | no | Appears in every authorisation URL |
| `GOOGLE_OAUTH_CLIENT_SECRET` | **yes** | Redeems authorisation codes for this client. API containers only |

Set both, or neither. With neither, Google sign-in is off: `GET /auth/browser/providers`
answers `google: false` and the sign-in page shows no button. One without the other
refuses to start.

**There is no redirect-URI variable.** The callback is always

```
<PUBLIC_APP_URL>/api/v1/auth/browser/google/callback
```

and Google compares it **byte for byte** with the client's registered list. A trailing
slash, `http` against `https`, or `www.` against the bare domain is a
`redirect_uri_mismatch`.

| Tier | `PUBLIC_APP_URL` comes from | Redirect URI to register |
| --- | --- | --- |
| Production | `compose.yml`: `https://${ARENA64_DOMAIN}` | `https://${ARENA64_DOMAIN}/api/v1/auth/browser/google/callback` — check the deployed `ARENA64_DOMAIN` (the example file says `arena64.gg`) before registering |
| Staging | `compose.yml`: `https://${ARENA64_DOMAIN}` | the staging domain, same path |
| Local | `PUBLIC_APP_URL` in `apps/api/.env.local` (default `http://localhost:3000`) | the origin the browser actually uses, same path — see §3.3 |

---

## 2. Google Cloud console checklist

Use one Google Cloud project **per tier**, or at minimum one OAuth client per tier.
Production's client must never list a staging or local redirect URI. That is the
cross-environment confusion the derived redirect URI exists to prevent.

**Consent screen** (Google Auth Platform → Branding, Audience, Data access)

- [ ] **App name:** `Arena64`. **User support email:** an address the team reads.
- [ ] **App domain:** home page `https://<domain>/`, and the privacy policy and terms
      URLs. Google requires both before the app can be published to all users.
- [ ] **Authorised domains:** the tier's registrable domain (e.g. `arena64.gg`).
- [ ] **Audience:** *External*.
- [ ] **Scopes:** `openid`, `…/auth/userinfo.email`, `…/auth/userinfo.profile`. Nothing
      else. These are Google's non-sensitive scopes; the platform requests no others.
- [ ] **Publishing status:** while *Testing*, only listed **test users** can sign in
      (Google caps the list at 100). Add every account used for §4. Publish to
      *In production* only after §4 passes. Google may ask for brand verification
      (name, logo, domain ownership) before the consent screen shows the app's branding.
      Follow the console's prompts. The basic scopes above do not need the review that
      sensitive scopes require.

**Client** (Google Auth Platform → Clients → Create client)

- [ ] **Application type:** *Web application*.
- [ ] **Authorised JavaScript origins:** leave empty. The browser never calls Google
      from script; the API does the exchange.
- [ ] **Authorised redirect URIs:** exactly the tier's URI from §1, and only that.
- [ ] Copy the **client ID** and **client secret** straight into the tier's secret
      store. Do not paste them into a ticket, a chat or a commit.

---

## 3. Configuring a tier

### 3.1 Production and staging

1. Add both values to the tier's env file (`production.env` / `staging.env`) on the host.
   `compose.yml` already passes them to the API containers, empty by default.
2. Restart **both** API replicas. A replica with the old configuration offers no button
   or rejects the callback, depending on which half of the flow reaches it.
3. Run §4.

**Turning it off:** empty both values and restart. Accounts created through Google keep
working through "forgot password" and email + password. Their Google link stays in
`users.external_identity` and works again as soon as Google is turned back on.

**Rotating the secret:** add a second secret to the client in the console, put it in the
env file, restart both replicas, confirm §4 step 6, then delete the old secret in the
console.

### 3.2 What never to do

- Never set `GOOGLE_OAUTH_CLIENT_SECRET` as a `VITE_` variable or a build argument.
- Never reuse production's client for staging or local.
- Never log or screenshot a callback URL. It carries a one-time `code` until it is spent.

### 3.3 Local

1. Create a separate *Web application* client, and add your Google account as a test
   user.
2. Set `PUBLIC_APP_URL` to the origin your browser uses for the app, and register that
   origin + `/api/v1/auth/browser/google/callback`.
3. Google accepts plain-HTTP redirect URIs **only for `localhost`**. If the console
   refuses your dev host (for example `app.localhost`), either use `http://localhost:<port>`
   for the Google test or front the dev server with an HTTPS tunnel and register the
   tunnel's URI.
4. Put the pair in `apps/api/.env.local` and restart the API.

---

## 4. Manual verification against real Google

Run on every tier after configuring it. Use two test users: **A**, a Google account
whose address has no Arena64 account, and **B**, a Google account whose address
*already* has an Arena64 account with a known password.

| # | Do | Expect |
| --- | --- | --- |
| 1 | Open `/login` | "Continue with Google" above the form; email/password form unchanged |
| 2 | Start as **A**, press *Cancel* at Google | Back on `/login` with "Google sign-in was cancelled." |
| 3 | Start as **A**, consent | `/register/google` naming A's address; no session yet |
| 4 | Choose a taken username | "Username taken"; still on the page |
| 5 | Choose a free username | Signed in, on the page you started from; account shows as verified |
| 6 | Sign out, start as **A** again | Signed straight in. No username step, and no new account in the admin console |
| 7 | Settings → Sessions | The Google session is listed like any other and can be revoked |
| 8 | Start as **B** | `/connect-google` naming B's address; **not** signed in |
| 9 | Enter a wrong password | The ordinary wrong-password message; nothing linked |
| 10 | Enter the right password | Signed in to B's **existing** account: same username, rating and history |
| 11 | Sign out, start as **B** again | Signed straight in to the same account |
| 12 | Start, wait more than 10 minutes at Google, consent | `/login` with "…expired…"; start again works |
| 13 | Start in one browser, copy the Google URL into another, consent there | The other browser lands on `/login` with "…expired…" |
| 14 | Deactivate or restrict a linked test account in the admin console, sign in with Google | Refused with the same message a password sign-in gets |
| 15 | Sign in with email + password, and with username + password | Both work as before |

Record the tier, the date, who ran it and the result of each row in the release notes.
Until a tier has passed all fifteen, treat Google sign-in there as **not verified**.

---

## 5. Troubleshooting

| Symptom | Cause |
| --- | --- |
| Google shows `redirect_uri_mismatch` | The registered URI differs from `PUBLIC_APP_URL` + the callback path; compare them character by character |
| Google shows "Access blocked: app not verified" / "not a test user" | The consent screen is in *Testing* and the account is not a listed test user |
| `/login?google=expired` every time | The binding cookie is not reaching the callback. Check that the callback is served on the same host as the app (`/api` through the edge) and that the browser is not blocking first-party cookies |
| `/login?google=failed` | The exchange or the ID token was refused. API logs carry `google_token_exchange_refused` (with Google's reason code), `google_id_token_rejected` or `google_sign_in_unverified` (with the failing stage) |
| `/login?google=unavailable` | The process has no client pair; check both variables reached **both** replicas |
| `/login?google=email_unverified` | Google did not vouch for the address and the identity is not linked. Expected; the person signs up with email + password instead |
