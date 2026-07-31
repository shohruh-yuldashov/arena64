"""The `EmailVerificationToken` entity — one issued verification link.

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

## The three ways a verification link stops working

    used         redeemed once, permanently — the one-time-use rule
    expired      the 24-hour window elapsed
    superseded   a newer token was issued and this one was invalidated

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

from app.modules.auth.domain.one_time_tokens import OneTimeToken


@dataclass(slots=True)
class EmailVerificationToken(OneTimeToken):
    """One issued verification link.

    Adds no fields and no behaviour to `OneTimeToken` — deliberately. The
    value of this class is that it is *not* a `PasswordResetToken`: the
    two are stored in different tables, are read by different
    repositories, and grant very different things, so a function that
    accepts one must not silently accept the other. See
    `one_time_tokens.py` on why that is a type distinction rather than a
    runtime check.
    """
