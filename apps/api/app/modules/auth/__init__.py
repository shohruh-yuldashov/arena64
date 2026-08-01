"""The `auth` bounded context — proof of identity.

Owns *credentials and the act of proving who you are*. `users` owns *who
you appear to be* (architecture.md §6 keeps them separate contexts, and
domain-model.md DM-10 explains why: a player can exist without an
account — a bot seat, a guest — so identity and credentials cannot be the
same aggregate).

## What this module does

Everything between "here is an email and a password" and "here is a proven
identity", across nine slices (A64-011.1 through .9):

    registration          validate the password policy, hash with
                          Argon2id, delegate account creation to `users`
    sign-in               verify credentials in constant time, refusing
                          identically for an unknown address and a wrong
                          password
    access tokens         short-lived signed JWTs — `AccessTokenService`
                          issues, `TokenValidator` verifies
    refresh sessions      opaque, stored, rotated on every use, with
                          reuse detection that revokes the whole chain
    email verification    single-use hashed links, one live per account
    password reset        the same, plus: replace the credential and
                          revoke every session
    rate limiting         a Redis sliding window over the six
                          unauthenticated endpoints
    the HTTP API          ten endpoints in `presentation/router.py`

Explicitly **not** here: OAuth, authorization (roles, scopes,
permissions), and WebSocket tickets (AD-09). None is stubbed — an empty
`authorize()` waiting to be filled in is worse than its absence, because
it reads as "supported" to the next person. `TokenType` shipping `ACCESS`
alone is the same rule applied at a smaller scale, and `domain/tokens.py`
says so.

## The boundary that matters

`auth` proves; `users` stores who you are. `auth` owns its own tables —
`auth.user_sessions`, `auth.email_verification_tokens`,
`auth.password_reset_tokens`, all in its own schema (database.md §3.1) —
and reaches `users` only through that module's published ports
(`UserAccountCreator`, `UserCredentialStore`, `UserProfileReader`,
`EmailVerifier`, `PasswordResetter`). Five narrow ports rather than one
wide one, so that creating an account, reading a password hash, reading a
profile, confirming an address and replacing a credential stay separately
grantable capabilities.

That split is what makes password policy and hashing cost changeable in
one place. It also means `password_hash` sitting on `users.user` is a
storage location, not an ownership claim — see
`users/infrastructure/models.py` for that documented deviation and the
path to splitting it.

A64-011.1's note that this module "never reads or writes the database" was
true of registration and stopped being true at A64-011.4, when a refresh
session became state `auth` alone creates, rotates and revokes.

## Layout

Mirrors `users` exactly (`domain` / `application` / `infrastructure` /
`presentation` / `public`), for the reason services.md §2.1 gives:
uniformity is worth more than local optimisation, because a contributor
who has read one module can navigate the next.
"""
