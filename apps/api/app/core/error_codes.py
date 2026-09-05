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

    # A64-018.3 — match history and replay.
    UNSUPPORTED_ENGINE_VERSION = "unsupported_engine_version"
    """The match was played under rules this build cannot reproduce, so its
    replay is refused rather than approximated (A64-014.8). Its own code
    because a client's response is specific: show the game's metadata and
    hide the replay control, which `conflict` alone could not say."""

    INVALID_CURSOR = "invalid_cursor"
    SESSION_ROTATION_CONFLICT = "session_rotation_conflict"
    """Two requests from one client presented the same refresh token.

    Distinct on the wire, against the rule that every *refresh failure*
    answers alike (`InvalidRefreshToken`). It earns the exception because
    this is not a refresh failure: the caller is who they say they are and
    their token was valid — it lost a race with their own other tab, and
    being told to try again is the only way they recover.

    What it discloses is that the presented token was rotated within the
    grace window. A legitimate client already knows that, having started the
    rotation; anybody else learns nothing they can use, because the answer
    carries no credential.
    """
    """A pagination cursor this API did not issue. Distinct from a generic
    validation error because the client's recovery is specific — ask for
    the first page — and it must not be confused with a bad filter."""

    # A64-019.8 — tournament registration and withdrawal.
    #
    # Six codes rather than `conflict` for all of them, and the test is the
    # one this enum's docstring applies: a client's *response* differs for
    # every one. "The field is full" offers a different tournament, "you
    # already entered" is a no-op the UI should reconcile, "registration
    # has closed" hides the button, and "the deadline passed" says when.
    # Collapsing them would make the client parse a message to decide.
    TOURNAMENT_NOT_FOUND = "tournament_not_found"
    REGISTRATION_NOT_OPEN = "registration_not_open"
    REGISTRATION_DEADLINE_PASSED = "registration_deadline_passed"
    TOURNAMENT_FULL = "tournament_full"
    ALREADY_REGISTERED = "already_registered"
    REGISTRATION_NOT_FOUND = "registration_not_found"
    INVALID_TOURNAMENT_STATE = "invalid_tournament_state"
    """A lifecycle command asked for a transition the aggregate refuses —
    seeding an open tournament, starting an unseeded one. One code for the
    family, because the operator's answer is always "look at the status"
    and naming each transition would publish the state machine."""

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

    # `matchmaking` (A64-015.5). **One** code, and it is earned by the rule
    # in this class's docstring rather than bending it: `POST
    # /matchmaking/queue` already answers `409` for "you are already
    # queued", and a decline cooldown is a second, entirely different `409`
    # on the same endpoint with a different client action.
    #
    #   AlreadyQueued          leave the queue you are in, or wait for the
    #                          match you already have
    #   QUEUE_COOLDOWN_ACTIVE  you cannot queue for a stated number of
    #                          seconds — show a countdown, disable the
    #                          button, retry after
    #
    # A client that could not tell them apart would render "you are already
    # in a queue" to somebody who is not in one, and would offer a "leave
    # queue" action that does nothing. That is precisely the case
    # `DUPLICATE_FRIEND_REQUEST` and `OPPOSITE_FRIEND_REQUEST_PENDING` were
    # split for.
    #
    # It names its own cause, unlike every other queue refusal, and that
    # asymmetry is deliberate — see `QueueCooldownActive` on why a bar the
    # player earned by their own action may be explained while one that
    # depends on the block graph may not.
    QUEUE_COOLDOWN_ACTIVE = "queue_cooldown_active"

    # `reference` (A64-020.5A-pre). **One** code, covering both ways a time
    # control can fail to be one this platform offers: no catalogue entry
    # matches the identifier, or the entry exists and has been retired.
    #
    # Earned rather than bent. `POST /matchmaking/queue` already answers
    # `422` for a malformed body, and a client's action there is "fix the
    # request". Here it is specifically *"the menu you are holding is stale
    # — read the catalogue again and re-render the picker"*, which is a
    # different behaviour and one a client cannot derive from the status
    # and the path.
    #
    # Unknown and retired share it for the reason `INVALID_VERIFICATION_TOKEN`
    # collapses three causes: the client's move is identical, and telling a
    # caller that an identifier names a *withdrawn* control rather than no
    # control announces a product decision through an error code.
    UNSUPPORTED_TIME_CONTROL = "unsupported_time_control"

    # `notifications` (A64-021.3). **Two** codes for two refusals that share
    # one endpoint, one method and one status — which is precisely when the
    # rule in this class's docstring grants them. `PATCH
    # /notifications/preferences` answers `422` for both, and a settings
    # screen's response differs:
    #
    #   NOTIFICATION_PREFERENCE_LOCKED       this switch is not yours to
    #                                        flip — restore it and explain
    #                                        that the platform must be able
    #                                        to reach you about your account
    #   NOTIFICATION_CHANNEL_UNAVAILABLE     the channel does not deliver in
    #                                        this build — restore it and say
    #                                        "coming soon", not "not allowed"
    #
    # Two genuinely different sentences to a player, and a client cannot
    # derive which from the status or the path. Note that neither is a
    # `403`: nothing about the *caller's* authority is in question, and a
    # permission error would send a client into its re-authentication path
    # for a switch that nobody may flip.
    #
    # A malformed body — an unknown category, a missing field — stays a
    # plain `validation_error`, because there the answer is "fix the
    # request" and no player-facing sentence exists.
    NOTIFICATION_PREFERENCE_LOCKED = "notification_preference_locked"
    NOTIFICATION_CHANNEL_UNAVAILABLE = "notification_channel_unavailable"

    DUPLICATE_PREFERENCE_CHANGE = "duplicate_preference_change"

    # `auth` (A64-021.5H). Six codes for the six-digit verification flow,
    # and the count is earned rather than bent: every one of them is a
    # different sentence and a different next action on one screen.
    #
    #   ..._CODE_INVALID          type the current code again
    #   ..._CODE_EXPIRED          retyping is pointless — ask for another
    #   ..._ATTEMPTS_EXCEEDED     the challenge is gone; ask for another
    #   ..._RESEND_TOO_SOON       wait, and the response says how long
    #   EMAIL_ALREADY_VERIFIED    there is nothing left to do
    #   ..._REQUIRED              this action needs a verified address
    #
    # The link endpoint keeps its single `INVALID_VERIFICATION_TOKEN` and
    # should: it is **unauthenticated**, so distinguishing its failures
    # reports on whether a token somebody holds was ever real. The code
    # endpoint is reached by a caller already proven to be this account, so
    # there is no account to enumerate and every distinction is one the
    # person needs.
    EMAIL_VERIFICATION_CODE_INVALID = "email_verification_code_invalid"
    EMAIL_VERIFICATION_CODE_EXPIRED = "email_verification_code_expired"
    EMAIL_VERIFICATION_ATTEMPTS_EXCEEDED = "email_verification_attempts_exceeded"
    EMAIL_VERIFICATION_RESEND_TOO_SOON = "email_verification_resend_too_soon"
    EMAIL_ALREADY_VERIFIED = "email_already_verified"

    EMAIL_VERIFICATION_REQUIRED = "email_verification_required"

    # --- friend challenges (A64-022.2) ---------------------------------------
    # Six codes for six different next moves. The rule that grants a code is
    # "a client's next action differs", and each of these genuinely does:
    #
    #   ..._SELF_NOT_ALLOWED      you named yourself — pick somebody else
    #   ..._NOT_FRIENDS           you cannot challenge them
    #   ..._ALREADY_PENDING       one already exists — answer or cancel it
    #   ..._NOT_PENDING           it was already answered — refresh
    #   ..._EXPIRED              too late — send another
    #   ..._INVALID_TIME_CONTROL  that clock is no longer offered
    #
    # **There is deliberately no `challenge_blocked`.** `domain-model.md`
    # §10.3, BL-2 and FR-2 require a challenge to a blocked player to fail
    # indistinguishably from one to a stranger, so a block raises
    # `CHALLENGE_NOT_FRIENDS` with the same message. A code that existed
    # would be the disclosure whatever sentence sat beside it.
    #
    # **No `challenge_forbidden` either.** A challenger who tries to decline
    # is a `403` with the platform's generic permission code; inventing a
    # challenge-specific one would say which of the two parties the caller
    # is, which the caller already knows and which nothing needs.
    CHALLENGE_SELF_NOT_ALLOWED = "challenge_self_not_allowed"
    CHALLENGE_NOT_FRIENDS = "challenge_not_friends"
    CHALLENGE_ALREADY_PENDING = "challenge_already_pending"
    CHALLENGE_NOT_PENDING = "challenge_not_pending"
    CHALLENGE_EXPIRED = "challenge_expired"
    CHALLENGE_INVALID_TIME_CONTROL = "challenge_invalid_time_control"
    """A write refused because the address is unconfirmed — `403`.

    Its own code because it is the one refusal a client answers by
    *navigating* rather than by retrying or re-authenticating: the fix is
    `/verify-email`, and a bare `permission_denied` would send the user
    looking for a permission nobody can grant them."""
    """One request named the same category and channel twice, so it has no
    single intent. Its own code because it is the one `422` on that endpoint
    a *client bug* produces rather than a player action — nothing on the
    settings screen can emit it, so a client that receives it should report
    it rather than render it as advice to the player."""
