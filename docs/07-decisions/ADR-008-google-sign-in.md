# ADR-008 — Google sign-in is OIDC by `sub`, and an address match never links an account

| Field | Value |
| --- | --- |
| **Status** | Proposed |
| **Date** | 2026-10-22 |
| **Deciders** | Shohruh |
| **Consulted** | — |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | `specs/authentication.md` §Google sign-in, `docs/01-architecture/domain-model.md` Q-22, `docs/01-architecture/database.md` §4.3, `docs/05-operations/google-sign-in.md`, `apps/api/app/modules/auth/application/services/google_sign_in_service.py` |

---

## Context

A64-031.E1 asks for "Continue with Google": register and sign in with a Google
account. `domain-model.md` had left this open as Q-22 — *is sign-in via an external
identity provider offered?* — noting that it "changes what 'one account per person' can
be enforced against, and what recovery means".

Three facts constrain the answer:

1. **Accounts already exist under addresses people also use at Google.** A player who
   registered `someone@gmail.com` with a password will, sooner or later, press
   "Continue with Google" with that same Gmail account.
2. **An address match is not proof of owning an Arena64 account.** Google vouches that
   the person controls the address *now*. It says nothing about who registered an
   Arena64 account with it: that could have been someone else, the account may never
   have been verified, or the address may have been recycled. Linking on the address
   alone hands an existing account — ratings, history, friends — to whoever holds the
   address at Google today. The reverse also happens: an attacker registers a victim's
   address first, and the victim's later Google sign-in lands in the attacker's account.
3. **The platform runs two API replicas.** The flow spans two browser round trips that
   routinely reach different processes.

Credentials already live on `users.user` (`email`, `password_hash`), not in the
`auth.credential` table `database.md` §4.3 drew, which was never built.

## Decision

> We will offer Google sign-in as the OpenID Connect authorization-code flow with PKCE
> and a nonce, identify the Google account **only by its `sub`**, and **never link an
> existing Arena64 account to a Google account because their addresses match** — linking
> requires that account's password in the same browser, within minutes of Google
> vouching for the address.

Specifically, once a callback has verified the ID token:

| The Google identity is… | Outcome |
| --- | --- |
| already linked (by `sub`) | Signed in to that account, after the same inactive / locked / restricted checks a password sign-in passes |
| unlinked, and its Google-verified address is unused on Arena64 | The person chooses a username; the account is created **verified** and linked in **one transaction** |
| unlinked, and an Arena64 account already uses the address | Nothing is linked or signed in. The person must enter **that account's** password; only then is Google linked |
| unlinked, with no verified address | Refused |

The link is stored in `users.external_identity`, with unique `(provider, subject)` and
unique `(user_id, provider)`. In-between state is kept in Redis, under the SHA-256 of
what the browser holds in `HttpOnly` cookies. Sessions are the existing ones.

## Options Considered

### Option 1 — Link only with the account's password *(chosen)*

**Summary:** On an address collision, park the verified Google identity and require the
existing account's password before linking.

| Pros | Cons |
| --- | --- |
| No takeover path through an address in either direction | One extra step for an existing user's first Google sign-in |
| The proof needed is one the person already has | Someone who forgot the password must reset it first, through the address Google just vouched for |
| Uses `AuthenticationService` unchanged: same timing, same throttle, same generic failure | |

### Option 2 — Auto-link when Google and Arena64 both report the address verified

**Summary:** Link silently if the Google claim is `email_verified` and the Arena64
account's `is_verified` is true.

| Pros | Cons |
| --- | --- |
| No extra step | Still links on the address: a recycled address, or one taken over at the provider, reaches the account |
| Common in consumer apps | Turns every account's security into the weaker of two providers' |
| | Explicitly ruled out by the A64-031.E1 brief |

### Option 3 — Link only from a signed-in settings page

**Summary:** On a collision, refuse. A user who wants Google signs in with the password
first, then presses "Connect Google" in settings.

| Pros | Cons |
| --- | --- |
| Equally safe | Needs a settings surface and an authenticated start route |
| | The colliding sign-in dead-ends, and the person has to work out the other path themselves |

### Option 4 — Do nothing

The platform stays password-only. Q-22 remains open, and the beta requirement goes unmet.

## Rationale

Account integrity outranks one click. Options 1 and 3 are equally safe, and Option 1
completes the person's own attempt instead of stranding it. It also needs no new
surface: the password step reuses the sign-in service whole. Option 2 is ruled out on
safety and by the brief.

The flow is standards-based rather than an SDK: authorization code with PKCE (RFC 7636)
and a nonce (OIDC Core §3.1.2.1). PyJWT, `cryptography` and `httpx` were already audited
dependencies. `sub` is the key because OIDC Core §2 makes it stable and never reassigned;
the address is neither.

The identity table lives in `users`, beside the password hash it complements. That is
what makes "an account created through Google, and its link" one transaction. Split
across modules, it would be two commits and a possible account nobody can reach.

## Consequences

### Positive

- Q-22 is answered, and "one account per person" holds through Google: one `sub` reaches
  one account, enforced by the database under concurrency.
- Google sign-in is no way around a sanction, a lock or a deactivation, and no way
  around the registration or sign-in rate limits.
- A Google user is an ordinary user afterwards: same session, cookie, rotation, device
  list and sign-out.

### Negative

- An existing password user's first Google sign-in asks for the password once.
- A Google-created account has no usable password until "forgot password" sets one.
  Recovery therefore depends on the address, as it does for every account.
- An account holds at most one Google identity. A second Google account cannot be added
  to the same Arena64 account.
- If Google stops vouching for an address that is not yet linked, that person cannot use
  Google here (they still can with a password).

### Neutral

- `PUBLIC_APP_URL` determines the redirect URI. The Google console must list exactly
  `PUBLIC_APP_URL` + `/api/v1/auth/browser/google/callback` for each tier.
- Unlinking Google, and linking it from settings, are not built. Neither is needed for
  the decision to hold.

## Impact

| Area | Impact |
| --- | --- |
| Architecture | Six routes under `/auth/browser`; no new session or token machinery |
| Data model | `users.external_identity` (migration `f5444391dfec`, additive); deviates from `database.md` §4.3 as that document now records |
| Security | Login CSRF, state replay, code interception, token substitution and account takeover by address are each closed — see `specs/authentication.md` §Google sign-in |
| Operations | `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` per tier; console setup in `docs/05-operations/google-sign-in.md` |
| Developer workflow | Off unless both variables are set; CI never contacts Google |

## Compliance & Enforcement

- `tests/unit/test_google_sign_in_service.py::TestAnUnlinkedIdentity::test_whose_address_an_account_uses_is_never_linked_on_the_address`
  fails if a collision links or signs in.
- `tests/contract/test_google_sign_in_api.py::TestAnAddressAnAccountAlreadyUses` asserts
  the same over HTTP against PostgreSQL.
- `tests/contract/test_external_identity_repository.py::TestTwoFirstSignInsAtOnce`
  asserts one account per `sub` under a real race.
- `tests/unit/test_google_oidc.py` covers each ID-token check.

## Follow-Up Actions

- [ ] Real-provider verification on staging with a test Google client — Shohruh, before enabling in production
- [ ] Decide whether "Connect Google" / "Disconnect Google" belong in account settings — Shohruh

## Revisit Criteria

- A second identity provider is added. The provider enum and constraints already admit
  it; this ADR's linking rule should be restated as provider-neutral.
- The platform introduces MFA (Q-21). A linked Google account would then need a policy
  on whether it satisfies the second factor.
- Support load from users stuck at the password step becomes material. The answer is
  probably Option 3 in addition to Option 1, not Option 2.

## References

- OpenID Connect Core 1.0 §2 (`sub`), §3.1.2.1 (`nonce`), §3.1.3.7 (ID token validation)
- RFC 7636 — Proof Key for Code Exchange
- RFC 6749 §4.1.2.1 — authorization error responses
- Google Identity — OpenID Connect: https://developers.google.com/identity/openid-connect/openid-connect
