"""The `EmailVerificationToken` entity — one issued verification challenge.

domain-model.md §6.1's account lifecycle has exactly one edge out of
`PendingVerification` into `Active`, labelled "email verified". This
entity is the credential that traverses it.

## What moved to `one_time_tokens.py`, and why

Everything mechanical. A64-011.7 needed an entity identical to this one in
every respect except its name and its table, so the expiry rule, the
one-time-use rule and the `issue` factory now live on `OneTimeToken` and
this class is what remains: a *name*, and the meaning that goes with it.

That is not a loss of documentation — the arguments for `>=` over `>`, for
"first consumption wins", and for a row rather than a signed value are all
still written down, once, on the base. What this file keeps is what is
true of verification links specifically.

## The four ways a verification challenge stops working

    used         redeemed once, permanently — the one-time-use rule
    expired      the window elapsed — 24 hours for a link, 10 minutes for
                 a code (`domain.otp`)
    superseded   a newer challenge was issued and this one was invalidated
    exhausted    A64-021.5H, and only for a code: five wrong guesses

The third is not a separate flag. `invalidate_previous_tokens` marks
superseded tokens *used*, because "cannot be redeemed again" is exactly
what `used_at` means and a fourth column would be a second answer to the
same question. What distinguishes them is `used_at` versus a redemption
that also flipped `is_verified` — and that distinction lives in the audit
log, not in this row.

## Why the same shape will serve email *change*

The task requires supporting "future email change verification", and this
entity already does, with one addition it deliberately does not make yet:
a `new_email` column. Verifying a *change* means proving control of an
address the account does not yet have, so the target address must travel
with the token rather than being read from the account. That column has no
writer today, and an unused nullable column on a credential table reads as
wired-up — see `TokenType` in `domain/tokens.py` for the same call. What
matters now is that nothing here *prevents* it: the token is keyed on
`user_id` and carries no assumption that the address it proves is the
account's current one.

Note that the column would go **here** rather than on `OneTimeToken`,
which is the second reason these are separate types: a password reset has
no target address, and a nullable `new_email` on the shared base would
appear on a table that can never fill it in.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.modules.auth.domain.one_time_tokens import OneTimeToken
from app.modules.auth.domain.otp import OTP_MAX_ATTEMPTS


class VerificationChallengeKind(StrEnum):
    """How the holder proves control of the address — A64-021.5H §4.

    Two, in one table and one state machine, because they are two ways of
    answering the same question and both end at the same `is_verified`.
    Two tables would be two "is there a live challenge" queries, two
    invalidation rules, and a window in which a person holds one of each.
    """

    LINK = "link"
    """A URL in an email — A64-011.6, and what every row written before
    A64-021.5H is. Kept working for links already in inboxes (§13); no new
    one is issued."""

    OTP = "otp"
    """Six digits the person types — the primary flow. `token_hash` holds
    a keyed verifier rather than a digest of a high-entropy token; see
    `domain.otp.otp_verifier` on why the distinction matters and why the
    column did not have to change."""


@dataclass(slots=True)
class EmailVerificationToken(OneTimeToken):
    """One issued verification challenge — a link or a code.

    Still not a `PasswordResetToken`: the two are stored in different
    tables, read by different repositories, and grant very different
    things, so a function that accepts one must not silently accept the
    other. See `one_time_tokens.py` on why that is a type distinction
    rather than a runtime check.

    ## Why `attempt_count` is here and not on the base

    A password reset token is a 32-byte random value in a URL. Guessing it
    is not a threat model, so counting guesses against it would be a column
    that never moves — and `one_time_tokens.py` already argues the same
    point about `new_email`: a field that one table can never fill in does
    not belong on a shared base.

    A six-digit code is the opposite. The attempt count is not bookkeeping;
    it is one of the three things that make a million-value secret safe
    (`domain.otp`).
    """

    kind: VerificationChallengeKind = VerificationChallengeKind.LINK
    """Defaulted to `LINK`, which is what every pre-existing row is and
    what the migration backfills. A default is right here rather than a
    required argument: the link path predates this field and should not
    have to name it."""

    attempt_count: int = 0
    """Failed guesses so far. Only ever moves for an `OTP` challenge, and
    only for a submission that was a plausible code — see
    `domain.otp.is_well_formed` on why a malformed field costs nothing."""

    @property
    def attempts_exhausted(self) -> bool:
        """Whether this challenge has been guessed at too many times.

        `>=` rather than `==`: two concurrent submissions can both increment
        before either reads, and a challenge at six attempts must be as
        finished as one at five. The database's own increment is what makes
        the count correct under concurrency; this is what reads it safely.
        """
        return self.attempt_count >= OTP_MAX_ATTEMPTS
