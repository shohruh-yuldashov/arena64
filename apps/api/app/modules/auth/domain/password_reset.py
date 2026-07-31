"""The `PasswordResetToken` entity — one issued password-reset link.

The credential that lets somebody who cannot sign in prove, by controlling
the registered address, that they may replace the password. It is the most
powerful bearer token this platform issues: redeeming one replaces a
credential and signs every device out, without the holder ever having
demonstrated knowledge of the old password.

Everything mechanical — expiry, one-time use, the `issue` factory — lives
on `OneTimeToken`, which this shares with `EmailVerificationToken`. Read
that module first; what follows is only what is true of *reset* links.

## Why the lifetime is an hour rather than a day

A verification link lasts 24 hours (`EmailSettings`) and this one lasts
one, and the asymmetry is the point rather than an inconsistency.

The two links sit in the same inbox and face the same threats — a
forwarded message, a mail gateway that logs URLs, a shared family
account — but they are worth very different amounts to whoever finds one.
A stolen verification link marks an address confirmed that its owner was
about to confirm anyway. A stolen reset link *is* the account.

So the windows are set by what a leak costs, not by symmetry. An hour is
long enough to survive a slow mail relay and a person who reads the email
on their phone and resets on their laptop; short enough that a message
sitting unread in a spam folder overnight is dead by morning, which is
precisely the outcome wanted. The recovery path for "it expired" is
another request to `/auth/password/forgot`, which is cheap and leaves an
audit trail — whereas the recovery path for "somebody else used it" does
not exist.

## The three ways a reset link stops working

    used         redeemed once, permanently — the one-time-use rule
    expired      the one-hour window elapsed
    superseded   a newer token was issued and this one was invalidated

As with verification, "superseded" is not a fourth column: it is `used_at`
set by `invalidate_previous_tokens`, because "cannot be redeemed again" is
exactly what that column means.

## What this entity deliberately does not carry

**No `requested_ip`.** database.md §4.5 lists one, and it is genuinely
useful — for the abuse analysis A64-011.8 will want, an address that
requested forty resets in a minute is the signal. The task's field list
does not include it, and adding a column that nothing reads to a table
holding personal data (§14.1) would be storing an IP address on the
strength of a plan rather than a caller. It is in the recommendations for
A64-011.8, which is the task that would read it.

**No record of the old password hash.** Nothing here can answer "was the
new password the same as the old one", and nothing should: answering it
requires keeping a credential the account no longer uses, which is the
liability §4.5 hard-deletes these rows to avoid.
"""

from dataclasses import dataclass

from app.modules.auth.domain.one_time_tokens import OneTimeToken


@dataclass(slots=True)
class PasswordResetToken(OneTimeToken):
    """One issued password-reset link.

    Adds no fields and no behaviour to `OneTimeToken` — deliberately. The
    value of this class is that it is *not* an `EmailVerificationToken`:
    a verification link must never be redeemable at
    `POST /auth/password/reset`, and making the two distinct types is what
    turns that from a check somebody has to remember into a type error.
    """
