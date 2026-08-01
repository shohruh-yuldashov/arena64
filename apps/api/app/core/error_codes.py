"""The error code registry — services.md §7.2 rule 4: every error carries
"a stable, machine-readable code" on the wire. Before this file, that code
was a bare string constant scattered across `exceptions.py` (`default_code:
ClassVar[str] = "not_found"`) — correct on the wire, but with no single
place enumerating every code that exists, which is what a frontend needs to
build an exhaustive `switch` over, and what an OpenAPI schema needs to
render as an enum instead of an unconstrained string.

One registry, `exceptions.py` references it, `exception_handlers.py`
serialises it, and `apps/web/src/types/api.ts` mirrors it by hand — the
one seam that can't be shared across a Python/TypeScript boundary without
a codegen step this platform doesn't have yet (a documented follow-up, not
a gap introduced silently).
"""

from enum import StrEnum


class ErrorCode(StrEnum):
    """Every code an `Arena64Error` can carry. Additive only — see
    `Arena64Error`'s own note on why: removing or renaming a member breaks
    every client that branches on it, including ones already deployed.

    **When a module earns its own code, and when it doesn't.** A module's
    exception class always exists (`UserNotFound`, `IllegalMove`) — that is
    what server-side code branches on. A *new wire code* is added here only
    when a client must be able to behave differently for it, and the HTTP
    status plus the endpoint it came from are not enough to tell it apart.
    `UserNotFound` on `GET /users/{id}` needs no code of its own — `404` +
    `not_found` already says everything a client can act on. But a `409`
    from a registration form must say *which* field collided, so
    `USERNAME_ALREADY_EXISTS` and `EMAIL_ALREADY_EXISTS` are distinct
    codes. Without that rule this enum grows a member per exception class
    per module and stops being a useful thing to exhaustively switch over.
    """

    # Arena64Error itself — the fallback when nothing more specific applies.
    INTERNAL_ERROR = "internal_error"

    # ValidationError
    VALIDATION_ERROR = "validation_error"

    # DomainError and its children — services.md BE-07: normal outcomes,
    # never logged as errors.
    DOMAIN_ERROR = "domain_error"
    AUTHENTICATION_FAILED = "authentication_failed"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    PERMISSION_DENIED = "permission_denied"
    PRECONDITION_FAILED = "precondition_failed"
    RULE_VIOLATION = "rule_violation"
    RATE_LIMITED = "rate_limited"

    # InfrastructureError and its children
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    TRANSIENT_INFRASTRUCTURE_ERROR = "transient_infrastructure_error"
    PERMANENT_INFRASTRUCTURE_ERROR = "permanent_infrastructure_error"

    # --- module-specific codes, per the rule in this class's docstring ---
    # `users` (A64-010): a sign-up or profile form must know which field
    # collided to highlight it; a bare `conflict` cannot say.
    USERNAME_ALREADY_EXISTS = "username_already_exists"
    EMAIL_ALREADY_EXISTS = "email_already_exists"

    # --- friends (A64-013.2) -------------------------------------------------
    # Two codes for two `409`s on the same endpoint, which is exactly when
    # the rule above grants one: `POST /friends/requests` can conflict for
    # two reasons and the client's next move is different for each.
    #
    #   DUPLICATE_FRIEND_REQUEST          you already asked — nothing to do
    #   OPPOSITE_FRIEND_REQUEST_PENDING   they asked you — accept that instead
    #
    # The second is genuinely actionable UI, and a client cannot derive it
    # from the status and the path.
    DUPLICATE_FRIEND_REQUEST = "duplicate_friend_request"
    OPPOSITE_FRIEND_REQUEST_PENDING = "opposite_friend_request_pending"

    # `auth` / `users` (A64-011.1): registration submits three fields at
    # once, and a bare `validation_error` leaves a form with no way to know
    # which of them to mark. These three are the same rule as above applied
    # to 422s rather than 409s — the client's behaviour genuinely differs
    # per code (which input to focus and annotate), which is the test.
    INVALID_USERNAME = "invalid_username"
    INVALID_EMAIL = "invalid_email"
    WEAK_PASSWORD = "weak_password"

    # `auth` (A64-011.2). Three genuinely different client behaviours:
    # retry the form, contact support, or wait and try later. Note that
    # `INVALID_CREDENTIALS` is deliberately the *only* one reachable
    # without already knowing the password — see
    # `auth/application/services/authentication_service.py` on why the
    # other two are not an account-enumeration oracle.
    INVALID_CREDENTIALS = "invalid_credentials"
    INACTIVE_ACCOUNT = "inactive_account"
    ACCOUNT_LOCKED = "account_locked"

    # `auth` (A64-011.3). Three codes for four exception types, and the
    # arithmetic is the rule in this docstring doing its job:
    #
    #   AUTHENTICATION_REQUIRED  no credential was presented — prompt for
    #                            sign-in; there is nothing to discard
    #   EXPIRED_TOKEN            the credential was ours and has aged out —
    #                            refresh it (A64-011.4) and retry, do *not*
    #                            send the user back to a sign-in form
    #   INVALID_TOKEN            the credential cannot be trusted — discard
    #                            it and sign in again
    #
    # `InvalidSignature` deliberately carries `INVALID_TOKEN` rather than a
    # code of its own. No client can act differently on "the signature was
    # forged" versus "the payload was malformed" — both mean *discard and
    # re-authenticate* — and telling a caller which one it was reports back
    # on the structural validity of their forgery attempt, which is a free
    # oracle for anyone probing the token format.
    AUTHENTICATION_REQUIRED = "authentication_required"
    INVALID_TOKEN = "invalid_token"
    EXPIRED_TOKEN = "expired_token"
    # `auth` (A64-011.4). Two codes for four exception types, and the
    # arithmetic is the rule in this docstring doing its job.
    #
    #   SESSION_EXPIRED     the session aged out or sat idle — the client
    #                       must sign in again, and can say *why* rather
    #                       than showing a bare error
    #   INVALID_SESSION     everything else: an unrecognised token, a
    #                       revoked session, a session that no longer
    #                       exists. Same client behaviour — discard the
    #                       stored token and sign in again
    #
    # `RevokedSession` and `SessionNotFound` deliberately share
    # `INVALID_SESSION`. Distinguishing them would tell whoever presented
    # the token whether it ever named a real session — which is a
    # membership oracle over the session table, and would let an attacker
    # holding a stolen-but-revoked token learn that revocation is what
    # stopped them rather than a bad guess.
    INVALID_SESSION = "invalid_session"
    SESSION_EXPIRED = "session_expired"

    # `auth` (A64-011.6). **One** code, covering every way a verification
    # link fails: unknown, already used, expired.
    #
    # Expiry deliberately does not get its own code, unlike
    # `SESSION_EXPIRED`. The client's action is identical for all three —
    # request a new link — and distinguishing "expired" from "unknown"
    # tells whoever is probing whether a token they hold was ever real,
    # which is a membership oracle over the token table. `SESSION_EXPIRED`
    # earns its own code because *there* the actions genuinely differ
    # (refresh versus sign in again).
    #
    # There is deliberately no `email_already_verified`. It was written
    # and removed: the resend endpoint is unauthenticated and must not
    # disclose verification state, and a valid token for an
    # already-verified account is unreachable while at most one token is
    # live per account. A wire code nothing can emit is a promise to
    # clients that the server cannot keep.
    INVALID_VERIFICATION_TOKEN = "invalid_verification_token"

    # `auth` (A64-011.7). **One** code, covering every way a password-reset
    # link fails: unknown, already used, expired. Same reasoning as
    # `INVALID_VERIFICATION_TOKEN` above — the client's action is identical
    # for all three, and distinguishing them is a membership oracle over
    # the token table.
    #
    # This one sits at the edge of the rule in this class's docstring, and
    # it is worth saying so rather than pretending otherwise. Strictly, a
    # client *can* tell a failed reset from a failed verification by the
    # endpoint it called, so by the letter of "the HTTP status plus the
    # endpoint are not enough" this code is not earned.
    #
    # It exists anyway because the alternative is worse than the rule it
    # bends. The remaining option is to return `invalid_verification_token`
    # from `POST /auth/password/reset`, and a wire code is a *name*: every
    # client keys a message table on it, every log search greps it, and a
    # reset failure filed under "verification" makes both wrong in a way
    # that costs an afternoon to work out. The rule exists to stop this
    # enum growing a member per exception class; one member per credential
    # *kind* is not that failure mode.
    INVALID_RESET_TOKEN = "invalid_reset_token"

    # `avatars` (A64-012.2). **One** code, for the one avatar rejection a
    # client can act on without human choice: an oversized file can be
    # re-encoded and retried automatically, where "not a supported format"
    # and "not a decodable image" both mean *ask the person for a different
    # file*. Those two share the generic `validation_error`, which the
    # message qualifies.
    AVATAR_TOO_LARGE = "avatar_too_large"
